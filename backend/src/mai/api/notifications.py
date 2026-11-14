"""Machine ingest lane + ingest-key management + the captain's global
read surface over the unified notification inbox (auth ADR §13;
docs/architecture/notification-inbox.md).

Ownership (notification-inbox campaign: tasks nbi-ingest-api +
nbi-read-api; the frozen sources are
``docs/architecture/notification-inbox.md`` and auth ADR ``§13``
machine ingest principal):

- ``POST /api/v1/notifications/ingest`` is the ONLY route that
  authenticates ``Authorization: Bearer <key>`` (RFC 6750 §2.1 header
  form). The lane is cookieless: no session, no Origin, no CSRF. G1 is
  preserved: in setup state it answers the value-free ``401`` like every
  other non-health route, BEFORE anything else.
- The request body budget (``MAX_INGEST_BODY_BYTES = 16384``) is enforced
  BEFORE payload validation AND BEFORE key validation: a Content-Length
  pre-check plus a bounded stream read. Over budget → the byte-identical
  ``413 body_too_large`` C01 envelope whether the Bearer header is valid,
  invalid or absent (no 401-vs-413 key-validity oracle); no row is
  written; a well-formed key still has the request counted against its
  durable rate window.
- Absent/malformed/unknown/revoked/expired credentials collapse into ONE
  value-free ``401 unauthenticated`` envelope (no key material, no
  existence oracle); the per-key fixed window answers ``429 throttled``;
  scope violations — ANY ``server_id`` on a global key, unknown OR
  foreign server for a scoped key, or a key whose own server profile is
  gone — answer the byte-identical ``422 invalid_scope`` envelope.
- Key management is the session-authenticated admin surface
  (``GET/POST /api/v1/ingest-keys``, ``POST /api/v1/ingest-keys/
  {key_id}/revoke``): mutations keep the session → origin → CSRF order
  (ADR §5); listings and mint receipts carry metadata only — plaintext is
  returned exactly once at mint and ``key_hash`` never leaves the store.
- The captain's read surface (task nbi-read-api) is the fleet-wide,
  session-authenticated view of the SAME durable inbox:
  ``GET /api/v1/notifications`` (one bounded page under the canonical
  filter set: ``server_id | scope=global`` mutually exclusive,
  ``include_global``, ``unread``, ``origin``, ``source``, ``q`` ILIKE
  over title/summary/body/source, ``limit``/``offset``),
  ``GET /api/v1/notifications/unread-count`` (``{total}`` under the
  exact same predicate), ``POST /api/v1/notifications/{alert_id}/ack``
  and ``.../{alert_id}/unack`` (bidirectional, idempotent,
  one truth column ``acknowledged_at``; evidence never deleted) and
  ``POST /api/v1/notifications/mark-all-read`` (ONE conditional UPDATE
  under the same filters; receipt ``{updated}`` = rows actually
  stamped). Safe reads run the session layer only; every mutation keeps
  session → origin → CSRF (ADR §5). The view model is
  ``mai.api.monitoring.AlertView`` verbatim, so the unified inbox renders
  ONE wire shape on every surface.

Every error uses the shared C01 envelope renderer; the returned plaintext
is never logged and never echoed by any error path.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Final, Literal

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_413_CONTENT_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_429_TOO_MANY_REQUESTS,
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
from mai.api.monitoring import AckResult, AlertView, _alert_view
from mai.auth.ingest_keys import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    IngestKey,
    IngestKeyError,
    IngestKeyErrorCode,
    IngestKeyStore,
)
from mai.contracts.observations import (
    MAX_INGEST_BODY_BYTES,
    MAX_NOTIFICATION_SOURCE_CHARS,
    AlertOrigin,
)
from mai.contracts.tags import compute_has_more
from mai.notifications.service import NotificationError, ingest_external
from mai.profiles.files import ProfileFileError, ProfileFileStore

__all__ = ["register_notifications"]


#: The ONE value-free 401 message of this lane — identical for absent,
#: malformed, unknown, revoked and expired credentials (no existence
#: oracle) and byte-stable across every auth failure.
_UNAUTHENTICATED_MESSAGE: Final[str] = "authentication required"

#: 503 vocabulary of this surface (notification-inbox.md §5): the ingest
#: lane cannot authenticate or persist without its storage.
_UNAVAILABLE_MESSAGE: Final[str] = "ingest storage is unavailable"


def _unauthenticated() -> BoundaryError:
    return BoundaryError(
        HTTP_401_UNAUTHORIZED, "unauthenticated", _UNAUTHENTICATED_MESSAGE
    )


def _invalid_scope() -> BoundaryError:
    """The ONE byte-identical scope rejection (R3-F3: no existence oracle).

    Unknown server, foreign server, garbage server id, global key with
    any server id and a scoped key whose own profile vanished all answer
    exactly this envelope.
    """
    return BoundaryError(
        HTTP_422_UNPROCESSABLE_CONTENT, "invalid_scope", "notification scope rejected"
    )


def _unavailable() -> BoundaryError:
    return BoundaryError(
        HTTP_503_SERVICE_UNAVAILABLE, "service_unavailable", _UNAVAILABLE_MESSAGE
    )


# ---------------------------------------------------------------------------
# Wire shapes (transport only; truth is the 0017 rows and the stores)
# ---------------------------------------------------------------------------


class IngestResult(BaseModel):
    """The ingest receipt: the committed (or replayed) row identity."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str
    disposition: str


class IngestKeyView(BaseModel):
    """One key's metadata — NEVER ``key_hash`` or any plaintext material."""

    model_config = ConfigDict(extra="forbid")

    key_id: str
    label: str
    server_id: str | None
    created_at: str
    last_used_at: str | None
    expires_at: str | None
    revoked_at: str | None


class IngestKeyPageView(BaseModel):
    """One bounded key list page (AGENTS pagination invariant)."""

    model_config = ConfigDict(extra="forbid")

    keys: list[IngestKeyView]
    limit: int
    offset: int
    total: int
    has_more: bool


class IssuedIngestKeyView(BaseModel):
    """The mint receipt: plaintext carried EXACTLY ONCE, then never again."""

    model_config = ConfigDict(extra="forbid")

    key: str
    key_id: str
    label: str
    server_id: str | None
    created_at: str
    last_used_at: str | None
    expires_at: str | None
    revoked_at: str | None


class CreateIngestKeyBody(BaseModel):
    """Mint payload: label (1..64), optional server scope, optional expiry."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=64)
    server_id: uuid.UUID | None = None
    expires_at: datetime | None = None


class RevokeIngestKeyResult(BaseModel):
    """Idempotent revoke receipt: ``revoked`` is False when nothing changed."""

    model_config = ConfigDict(extra="forbid")

    key_id: str
    revoked: bool


class IngestPayload(BaseModel):
    """External notice payload (validated AFTER budget AND key validation).

    ``server_id`` stays a raw string so the scope gate can answer the
    byte-identical ``invalid_scope`` for ANY supplied value — including
    non-UUID garbage — instead of the generic validation envelope.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    body: str | None = None
    severity: str | None = None
    server_id: str | None = None
    source: str | None = None
    dedup_key: str | None = None
    context_ref: str | None = None


class NotificationPage(BaseModel):
    """One bounded global inbox page (AGENTS pagination invariant).

    ``alerts`` carries the shared :class:`mai.api.monitoring.AlertView`
    (additive fields only: nullable ``server_id`` for global notices,
    nullable ``source``/``body``); the envelope is the canonical
    ``limit``/``offset``/``total``/``has_more`` page.
    """

    model_config = ConfigDict(extra="forbid")

    alerts: list[AlertView]
    limit: int
    offset: int
    total: int
    has_more: bool


class UnreadCountView(BaseModel):
    """The unread badge/count receipt under the canonical filters."""

    model_config = ConfigDict(extra="forbid")

    total: int


class MarkAllReadResult(BaseModel):
    """The mark-all-read receipt: rows ACTUALLY stamped by the one UPDATE."""

    model_config = ConfigDict(extra="forbid")

    updated: int


# ---------------------------------------------------------------------------
# Accessors and small helpers
# ---------------------------------------------------------------------------


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """The lifespan-built session factory; without it fail closed."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise _unavailable()
    return factory


def _wire_ts(value: datetime) -> str:
    """Render one stored UTC timestamp in the frozen ``…Z`` wire form."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _wire_ts_or_none(value: datetime | None) -> str | None:
    return None if value is None else _wire_ts(value)


def _key_view(record: IngestKey) -> IngestKeyView:
    return IngestKeyView(
        key_id=str(record.key_id),
        label=record.label,
        server_id=None if record.server_id is None else str(record.server_id),
        created_at=_wire_ts(record.created_at),
        last_used_at=_wire_ts_or_none(record.last_used_at),
        expires_at=_wire_ts_or_none(record.expires_at),
        revoked_at=_wire_ts_or_none(record.revoked_at),
    )


def _bearer_token(request: Request) -> str | None:
    """The ``Authorization: Bearer <token>`` credential (RFC 6750 §2.1).

    ``None`` for every absent/malformed form — one value-free 401 for all.
    """
    header = request.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = credentials.strip()
    return token or None


def _declared_content_length(request: Request) -> int | None:
    """The parsed ``Content-Length`` declaration, if usable."""
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _read_bounded(request: Request) -> bytes | None:
    """Bounded stream read; ``None`` once the body passes the budget.

    Reading stops at the first chunk that crosses ``MAX_INGEST_BODY_BYTES``
    — the remaining bytes are never buffered (the answer is already 413).
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_INGEST_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _answer_oversized(
    request: Request, factory: async_sessionmaker[AsyncSession]
) -> JSONResponse:
    """The byte-identical ``413 body_too_large`` envelope.

    Decided BEFORE key validation, so the envelope never depends on key
    validity. A well-formed Bearer credential still has the request
    counted against its durable rate window; every store rejection
    (unknown/invalid/revoked/expired/exhausted) is swallowed — there is
    no 401-vs-413 and no 429-vs-413 oracle.
    """
    token = _bearer_token(request)
    if token is not None:
        try:
            await IngestKeyStore(factory).consume_rate_window(token)
        except IngestKeyError:
            pass  # value-free: the 413 outcome is key-independent
    return render_error_envelope(
        request,
        HTTP_413_CONTENT_TOO_LARGE,
        "body_too_large",
        "request body exceeds the ingest body budget",
    )


def _validation_failed(request: Request) -> JSONResponse:
    """The constant payload-validation envelope (bounds/shape/JSON)."""
    return render_error_envelope(
        request,
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "request body failed validation",
    )


def _profile_exists(request: Request, server_id: uuid.UUID) -> bool:
    """Whether the canonical profile file exists for ``server_id``.

    Any store rejection (absent, tampered, confined, unreadable) is one
    ``False`` — the ingest scope gate never distinguishes them.
    """
    profiles = getattr(request.app.state, "profiles", None)
    if profiles is None:
        raise _unavailable()
    try:
        profiles.read(server_id)
    except (ProfileFileError, OSError):
        return False
    return True


def _ilike_pattern(q: str) -> str:
    """Literal substring pattern for one ``q`` value (R2-F1 field set).

    ``%``/``_``/``\\`` are escaped so the captain's search text never
    becomes a wildcard; the SQL side pairs this with ``ESCAPE '\\'``.
    """
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _canonical_filters(
    *,
    server_id: uuid.UUID | None,
    scope: str | None,
    include_global: bool,
    unread: bool | None,
    origin: AlertOrigin | None,
    source: str | None,
    q: str | None,
) -> tuple[str, dict[str, object]]:
    """The ONE canonical WHERE clause over the unified inbox (R4-F2).

    Frozen semantics, shared VERBATIM by the list, unread-count and
    mark-all-read routes so totals and effects can never disagree:

    - no ``server_id`` and no ``scope`` → ALL notices (every server plus
      global); ``include_global`` is ignored (nothing to exclude);
    - ``server_id`` → that server plus global rows unless
      ``include_global=False`` (then strictly that server);
    - ``scope='global'`` → only ``server_id IS NULL`` rows;
      ``server_id`` and ``scope`` are mutually exclusive → 422.
    - ``unread`` composes like on the list (``True`` → unacknowledged
      only, ``False`` → acknowledged only);
    - ``q`` is ONE case-insensitive ILIKE field set over ``title``,
      ``summary``, ``body`` and ``source`` (wildcards escaped).
    """
    if server_id is not None and scope is not None:
        raise BoundaryError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "server_id and scope are mutually exclusive",
        )
    clauses: list[str] = []
    params: dict[str, object] = {}
    if scope is not None:  # the only Query-legal value is 'global'
        clauses.append("server_id IS NULL")
    elif server_id is not None:
        params["f_server_id"] = server_id
        if include_global:
            clauses.append("(server_id = :f_server_id OR server_id IS NULL)")
        else:
            clauses.append("server_id = :f_server_id")
    if unread is not None:
        clauses.append(
            "acknowledged_at IS NULL" if unread else "acknowledged_at IS NOT NULL"
        )
    if origin is not None:
        clauses.append("origin = :f_origin")
        params["f_origin"] = origin.value
    if source is not None:
        clauses.append("source = :f_source")
        params["f_source"] = source
    if q:
        clauses.append(
            "(title ILIKE :f_q ESCAPE '\\'"
            " OR summary ILIKE :f_q ESCAPE '\\'"
            " OR COALESCE(body, '') ILIKE :f_q ESCAPE '\\'"
            " OR COALESCE(source, '') ILIKE :f_q ESCAPE '\\')"
        )
        params["f_q"] = _ilike_pattern(q)
    where = " AND ".join(clauses) if clauses else "TRUE"
    return where, params


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["notifications"])


@router.post(
    "/notifications/ingest",
    summary="Append one external notice (machine ingest principal, ADR §13)",
    response_model=IngestResult,
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": (
                "Absent/malformed/unknown/revoked/expired credential or "
                "setup state (value-free, no existence oracle)"
            ),
        },
        HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorEnvelope,
            "description": (
                "Body over MAX_INGEST_BODY_BYTES (decided before payload "
                "AND key validation; rate window still counts)"
            ),
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Payload bound violation or scope violation",
        },
        HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorEnvelope,
            "description": "Per-key rate window exhausted (60/minute)",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Ingest storage unavailable",
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title"],
                        "properties": {
                            "title": {"type": "string", "minLength": 1},
                            "body": {"type": ["string", "null"], "maxLength": 4096},
                            "severity": {
                                "type": ["string", "null"],
                                "enum": ["info", "warning", "critical", None],
                            },
                            "server_id": {"type": ["string", "null"]},
                            "source": {"type": ["string", "null"], "maxLength": 64},
                            "dedup_key": {"type": ["string", "null"]},
                            "context_ref": {"type": ["string", "null"]},
                        },
                    }
                }
            },
        }
    },
)
async def ingest_notification(request: Request) -> IngestResult | JSONResponse:
    """Persist one external notice through the Bearer machine lane.

    Frozen ordering (auth ADR §13 rule 3; RESEARCH §R3-F2/§R3-F3):
    G1 setup state → body budget → key validation (rate window) → payload
    validation → scope enforcement → dedup insert. Success returns the
    committed row identity; an idempotent replay answers ``200`` with
    ``disposition: "deduped"``.
    """
    factory = _session_factory(request)
    # G1 (auth ADR §8): setup state refuses this lane like every other
    # non-health route — before the budget, before any key validation.
    credentials = getattr(request.app.state, "sessions", None)
    if credentials is None:
        raise _unavailable()
    if not await credentials.credential_exists():
        raise _unauthenticated()

    # Body budget (16384 bytes): Content-Length pre-check, then a bounded
    # stream read — both BEFORE payload validation AND key validation.
    declared = _declared_content_length(request)
    if declared is not None and declared > MAX_INGEST_BODY_BYTES:
        return await _answer_oversized(request, factory)
    body_bytes = await _read_bounded(request)
    if body_bytes is None:
        return await _answer_oversized(request, factory)

    # Credential: one value-free 401 for absent/malformed/unknown/revoked/
    # expired; the durable per-key window answers 429.
    token = _bearer_token(request)
    if token is None:
        raise _unauthenticated()
    keys = IngestKeyStore(factory)
    try:
        record = await keys.validate(token)
    except IngestKeyError as exc:
        if exc.code is IngestKeyErrorCode.RATE_WINDOW_EXHAUSTED:
            raise BoundaryError(
                HTTP_429_TOO_MANY_REQUESTS,
                "throttled",
                "ingest rate window exhausted",
            ) from None
        raise _unauthenticated() from None

    # Payload shape and bounds (service-owned bounds; unknown fields
    # rejected; the submitted values are never echoed back).
    try:
        parsed = json.loads(body_bytes)
    except ValueError:
        return _validation_failed(request)
    if not isinstance(parsed, dict):
        return _validation_failed(request)
    try:
        draft = IngestPayload.model_validate(parsed)
    except ValidationError:
        return _validation_failed(request)

    # Scope enforcement (R3-F3): one byte-identical 422 invalid_scope for
    # every violation — ANY server_id on a global key; foreign, unknown
    # or unparsable server_id for a scoped key; a scoped key whose own
    # server profile no longer exists.
    if record.server_id is None:
        if draft.server_id is not None:
            raise _invalid_scope()
        target = None
    else:
        target = record.server_id
        if draft.server_id is not None:
            try:
                supplied = uuid.UUID(draft.server_id)
            except ValueError:
                raise _invalid_scope() from None
            if supplied != record.server_id:
                raise _invalid_scope()
        if not _profile_exists(request, target):
            raise _invalid_scope()

    # Dedup insert (idempotent replay → deduped, 200 either way).
    try:
        outcome = await ingest_external(
            factory,
            server_id=None if target is None else str(target),
            title=draft.title,
            body=draft.body,
            severity=draft.severity if draft.severity is not None else "info",
            source=draft.source if draft.source is not None else record.label,
            dedup_key=draft.dedup_key,
            context_ref=draft.context_ref,
        )
    except NotificationError:
        return _validation_failed(request)
    return IngestResult(
        alert_id=str(outcome.alert_id), disposition=outcome.disposition
    )


@router.get(
    "/ingest-keys",
    summary="One bounded ingest-key metadata page (never key material)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session (Bearer is not accepted here)",
        },
    },
)
async def list_ingest_keys(
    request: Request,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> IngestKeyPageView:
    """List ingest keys: label/scope/timestamps only, deterministically.

    Session-authenticated admin surface (layer 1 only; safe method). The
    page envelope carries ``limit``/``offset``/``total``/``has_more``;
    ``key_hash`` and plaintext never leave the store.
    """
    await _require_session(request)
    factory = _session_factory(request)
    page = await IngestKeyStore(factory).list_keys(limit=limit, offset=offset)
    return IngestKeyPageView(
        keys=[_key_view(record) for record in page.items],
        limit=page.limit,
        offset=page.offset,
        total=page.total,
        has_more=page.has_more,
    )


@router.post(
    "/ingest-keys",
    status_code=HTTP_201_CREATED,
    summary="Mint one ingest key (plaintext returned exactly once)",
    response_model=IssuedIngestKeyView,
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
            "description": "Payload failed validation (label/scope/expiry)",
        },
    },
)
async def create_ingest_key(
    request: Request, payload: CreateIngestKeyBody
) -> IssuedIngestKeyView | JSONResponse:
    """Mint one machine ingest key (auth ADR §13 key lifecycle).

    session → origin → CSRF (ADR §5). A supplied ``server_id`` must name
    an existing server profile; omitting it mints a GLOBAL key. The
    plaintext is carried by exactly this one response — only the SHA-256
    hash is stored, and no error path ever echoes key material.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _session_factory(request)
    if payload.server_id is not None and not _profile_exists(
        request, payload.server_id
    ):
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "request body failed validation",
            fields={"server_id": "invalid"},
        )
    try:
        issued = await IngestKeyStore(factory).mint(
            payload.label,
            server_id=payload.server_id,
            expires_at=payload.expires_at,
        )
    except IngestKeyError:
        return _validation_failed(request)
    return IssuedIngestKeyView(
        key=issued.key,
        **_key_view(issued.record).model_dump(),
    )


@router.post(
    "/ingest-keys/{key_id}/revoke",
    summary="Revoke one ingest key (effective on the next ingest)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
    },
)
async def revoke_ingest_key(
    request: Request, key_id: uuid.UUID
) -> RevokeIngestKeyResult:
    """Revoke one key; the next ingest fails closed (value-free 401).

    session → origin → CSRF (ADR §5). Idempotent: revoking an unknown or
    already-revoked key changes nothing and reports ``revoked: false`` —
    the receipt deliberately does not distinguish the two (no existence
    oracle), and the audit row is never reactivated.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _session_factory(request)
    revoked = await IngestKeyStore(factory).revoke(key_id)
    return RevokeIngestKeyResult(key_id=str(key_id), revoked=revoked)


# ---------------------------------------------------------------------------
# Captain read surface (unified inbox; docs/architecture/notification-inbox.md §3)
# ---------------------------------------------------------------------------

#: One full notice row by id (the ``_alert_view`` input shape); the
#: global surface is fleet-wide, so unlike the monitoring loader there is
#: deliberately NO server scope in this predicate — the captain reads
#: every notice (C01 rule 2 exception recorded in contracts.md).
_NOTICE_ROW_SQL = text(
    "SELECT alert_id, server_id, origin, dedup_key, severity,"
    " title, summary, source, body, context_ref, delivery_status,"
    " created_at, delivered_at, acknowledged_at, finding_id, run_id"
    " FROM notification_alerts WHERE alert_id = :alert_id"
)

async def _read_page(
    request: Request,
    filters: Mapping[str, object],
    *,
    limit: int,
    offset: int,
) -> NotificationPage:
    """One bounded inbox page under the canonical predicate.

    ``filters`` carries exactly the canonical filter surface (the route
    hands its validated Query values over verbatim);
    :func:`_canonical_filters` owns every semantics decision, so this
    helper is pure plumbing over the one shared predicate.
    """
    factory = _session_factory(request)
    where, params = _canonical_filters(
        server_id=filters["server_id"],  # type: ignore[arg-type]
        scope=filters["scope"],  # type: ignore[arg-type]
        include_global=filters["include_global"],  # type: ignore[arg-type]
        unread=filters["unread"],  # type: ignore[arg-type]
        origin=filters["origin"],  # type: ignore[arg-type]
        source=filters["source"],  # type: ignore[arg-type]
        q=filters["q"],  # type: ignore[arg-type]
    )
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT alert_id, server_id, origin, dedup_key, severity,"
                        " title, summary, source, body, context_ref, delivery_status,"
                        " created_at, delivered_at, acknowledged_at, finding_id, run_id"
                        " FROM notification_alerts"
                        f" WHERE {where}"
                        " ORDER BY created_at DESC, alert_id DESC"
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
                text(f"SELECT COUNT(*) FROM notification_alerts WHERE {where}"),
                params,
            )
        ).scalar_one()
    return NotificationPage(
        alerts=[_alert_view(row) for row in rows],
        limit=limit,
        offset=offset,
        total=int(total),
        has_more=compute_has_more(offset, len(rows), int(total)),
    )


@router.get(
    "/notifications",
    status_code=HTTP_200_OK,
    response_model=NotificationPage,
    summary="One bounded global inbox page (canonical filters, newest first)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": (
                "Query parameters failed validation (bounds, scope "
                "vocabulary, or server_id together with scope=global)"
            ),
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Notification storage unavailable",
        },
    },
)
async def list_notifications(
    request: Request,
    server_id: uuid.UUID | None = Query(default=None),
    scope: Literal["global"] | None = Query(default=None),
    include_global: bool = Query(default=True),
    unread: bool | None = Query(default=None),
    origin: AlertOrigin | None = Query(default=None),
    source: str | None = Query(
        default=None, max_length=MAX_NOTIFICATION_SOURCE_CHARS
    ),
    q: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> NotificationPage:
    """One bounded page of the unified inbox, newest first.

    Safe method: session layer only; the captain's scope is the whole
    fleet (explicit filters, never ambient server state). Deterministic
    order ``created_at DESC, alert_id DESC``; ``COUNT(*)`` runs under the
    EXACT same predicate as the slice, and ``has_more`` is the canonical
    derivation — pagination is honest under every filter combination.
    """
    await _require_session(request)  # layer 1
    return await _read_page(
        request,
        {
            "server_id": server_id,
            "scope": scope,
            "include_global": include_global,
            "unread": unread,
            "origin": origin,
            "source": source,
            "q": q,
        },
        limit=limit,
        offset=offset,
    )


@router.get(
    "/notifications/unread-count",
    status_code=HTTP_200_OK,
    response_model=UnreadCountView,
    summary="Count unread notices under the exact canonical filters",
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
            "description": "Notification storage unavailable",
        },
    },
)
async def count_unread_notifications(
    request: Request,
    server_id: uuid.UUID | None = Query(default=None),
    scope: Literal["global"] | None = Query(default=None),
    include_global: bool = Query(default=True),
    unread: bool | None = Query(default=None),
    origin: AlertOrigin | None = Query(default=None),
    source: str | None = Query(
        default=None, max_length=MAX_NOTIFICATION_SOURCE_CHARS
    ),
    q: str | None = Query(default=None),
) -> UnreadCountView:
    """The badge/drawer count: ``COUNT(*)`` under the list's predicate,
    pinned to UNACKNOWLEDGED notices.

    The EXACT same filter surface as the list route, so a count can never
    disagree with the page it belongs to — but the count is an UNREAD
    count by definition: the route's own base clause is ``acknowledged_at
    IS NULL``, added UNDER the canonical filters. ``unread`` composes on
    top like on the list: omitted/``true`` is the same restriction;
    ``unread=false`` is a contradiction (no notice is both read and
    unread) and answers ``0`` — never a silently flipped semantics.
    """
    await _require_session(request)  # layer 1
    factory = _session_factory(request)
    where, params = _canonical_filters(
        server_id=server_id,
        scope=scope,
        include_global=include_global,
        unread=unread,
        origin=origin,
        source=source,
        q=q,
    )
    if where == "TRUE":
        where = "acknowledged_at IS NULL"
    else:
        where = f"({where}) AND acknowledged_at IS NULL"
    async with factory() as session:
        total = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM notification_alerts WHERE {where}"),
                params,
            )
        ).scalar_one()
    return UnreadCountView(total=int(total))


@router.post(
    "/notifications/{alert_id}/ack",
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
            "description": "No notice for this id (value-free, scope-blind)",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Notification storage unavailable",
        },
    },
)
async def acknowledge_notification(
    request: Request, alert_id: uuid.UUID
) -> AckResult:
    """Stamp ``acknowledged_at`` on one notice; delete nothing.

    Mutation: session → origin → CSRF (ADR §5 order) before any state is
    touched. The captain's surface is fleet-wide, so the value-free 404
    is scope-blind by construction: an unknown id is the only failure.
    Idempotency rides one truth column: ``acknowledged_at =
    COALESCE(acknowledged_at, :now)`` — the first stamp wins, a replay
    never re-stamps or shifts the timestamp, and the notice, its finding
    and its producing observation all survive the ack.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _session_factory(request)
    now = datetime.now(timezone.utc)
    async with factory() as session:
        stamped = await session.execute(
            text(
                "UPDATE notification_alerts"
                " SET acknowledged_at = COALESCE(acknowledged_at, :now)"
                " WHERE alert_id = :alert_id"
            ),
            {"now": now, "alert_id": alert_id},
        )
        if stamped.rowcount != 1:
            raise BoundaryError(
                HTTP_404_NOT_FOUND,
                "not_found",
                "no notice for this id",
            )
        row = (
            await session.execute(_NOTICE_ROW_SQL, {"alert_id": alert_id})
        ).mappings().one()
        await session.commit()
    return AckResult(alert=_alert_view(row))


@router.post(
    "/notifications/{alert_id}/unack",
    status_code=HTTP_200_OK,
    response_model=AckResult,
    summary="Unacknowledge one inbox notice (re-arms reminders, idempotent)",
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
            "description": "No notice for this id (value-free, scope-blind)",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Notification storage unavailable",
        },
    },
)
async def unacknowledge_notification(
    request: Request, alert_id: uuid.UUID
) -> AckResult:
    """Clear ``acknowledged_at`` on one notice; delete nothing.

    Mutation: session → origin → CSRF (ADR §5 order) before any state is
    touched. Bidirectional ack on ONE truth column: ``acknowledged_at =
    NULL`` is idempotent (a replay changes nothing and answers ``200``)
    and re-arms finding reminder logic — the notification service stops
    suppressing re-delivery once the notice is unacknowledged again.
    Evidence is never deleted: the notice and its anchors survive.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _session_factory(request)
    async with factory() as session:
        cleared = await session.execute(
            text(
                "UPDATE notification_alerts SET acknowledged_at = NULL"
                " WHERE alert_id = :alert_id"
            ),
            {"alert_id": alert_id},
        )
        if cleared.rowcount != 1:
            raise BoundaryError(
                HTTP_404_NOT_FOUND,
                "not_found",
                "no notice for this id",
            )
        row = (
            await session.execute(_NOTICE_ROW_SQL, {"alert_id": alert_id})
        ).mappings().one()
        await session.commit()
    return AckResult(alert=_alert_view(row))


@router.post(
    "/notifications/mark-all-read",
    status_code=HTTP_200_OK,
    response_model=MarkAllReadResult,
    summary="Mark every filtered unread notice read (ONE conditional UPDATE)",
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
            "description": "Query parameters failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Notification storage unavailable",
        },
    },
)
async def mark_all_read(
    request: Request,
    server_id: uuid.UUID | None = Query(default=None),
    scope: Literal["global"] | None = Query(default=None),
    include_global: bool = Query(default=True),
    unread: bool | None = Query(default=None),
    origin: AlertOrigin | None = Query(default=None),
    source: str | None = Query(
        default=None, max_length=MAX_NOTIFICATION_SOURCE_CHARS
    ),
    q: str | None = Query(default=None),
) -> MarkAllReadResult:
    """Stamp ``acknowledged_at = now`` on every matching unread notice.

    Mutation: session → origin → CSRF (ADR §5 order). The canonical
    filters arrive as QUERY parameters (the same surface and semantics
    as the list route — no second body dialect to keep in sync). ONE
    UPDATE runs under the exact canonical predicate AND ``acknowledged_at
    IS NULL``; ``updated`` is the database's own rowcount — the receipt
    equals the rows actually stamped, never an optimistic guess. The
    first-stamp-wins rule holds: already-read rows are not re-stamped.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _session_factory(request)
    where, params = _canonical_filters(
        server_id=server_id,
        scope=scope,
        include_global=include_global,
        unread=unread,
        origin=origin,
        source=source,
        q=q,
    )
    now = datetime.now(timezone.utc)
    async with factory() as session:
        stamped = await session.execute(
            text(
                "UPDATE notification_alerts SET acknowledged_at = :now"
                f" WHERE {where} AND acknowledged_at IS NULL"
            ),
            params | {"now": now},
        )
        updated = int(stamped.rowcount)
        await session.commit()
    return MarkAllReadResult(updated=updated)


def register_notifications(app: FastAPI) -> None:
    """Register the ingest lane, key management and the read surface."""
    app.include_router(router)
