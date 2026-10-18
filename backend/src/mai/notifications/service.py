"""Durable web notifications over the 0007 observation schema (card 10-notifications, C10 + C03).

The notification service is the named consumer of the collector cards'
findings (``observation_findings``) and of durable job outcomes: it
evaluates conditions into ``notification_alerts`` inbox rows (C10 rule 3),
one row per ``(server_id, dedup_key)`` — the UNIQUE backstop makes a
repeated poll of the same condition collapse onto the same notice, and a
duplicate key is a real integrity violation, never a caller invariant.

Evaluation pipeline, per condition:

- **Deduplication** — the notice's dedup key is a deterministic bounded
  identity of the condition (a hash of the finding's ``(kind, dedup_key)``
  pair, or the job-result run id). Repeated evaluation of an open finding
  never stacks a second row; the structural UNIQUE is the last line of
  defense, not the mechanism.
- **Cooldown** — a still-open, delivered, unacknowledged condition is
  re-delivered (a repeat reminder) only after :attr:`RoutingPolicy.cooldown`
  has passed since its last delivery; inside the window re-evaluation is a
  recorded no-op. A severity escalation re-delivers immediately regardless
  of the cooldown (escalation is never throttled); an acknowledged notice is
  never nagged again.
- **Quiet hours** — outbound (webhook) delivery of non-critical notices is
  deferred inside the configured window: the notice persists immediately and
  honestly (``pending``) and :func:`deliver_pending` flushes it after the
  window. ``critical`` bypasses quiet hours. The web inbox itself has no
  quiet hours — inbox presence is the persisted row.
- **Severity routing** — :class:`RoutingPolicy` maps the frozen severity
  vocabulary (``info | warning | critical``,
  :class:`mai.contracts.observations.FindingSeverity`) to named delivery
  channels; a severity with no mapped channel is inbox-only (delivered the
  moment the row exists). Unknown severity keys or channel names are
  rejected at policy resolution, never silently dropped.
- **Durable delivery attempts** — the attempt's outcome is persisted in the
  row's frozen lifecycle (``pending | delivered | failed`` with the
  ``delivered_at`` stamp pinned by CHECK): every transition is its own
  committed UPDATE, so a crash leaves an honest state, and the pending
  sweep (:func:`deliver_pending`) is the resume path. A partially failed
  outbound round reports ``failed`` — the row stays visible in the inbox
  (inbox presence IS the row; delivery failure never deletes evidence).
- **SSE event emission** — after durable persistence the service emits the
  frozen event envelope shape (C05 rule 4: ``seq/ts/type/v/data``, open
  type vocabulary, bounded ``data``) through the :class:`EventSink` seam:
  ``notification.created`` (exactly once per notice) and the terminal
  ``notification.delivered`` / ``notification.failed`` (once per completed
  delivery round). The sink owns ordering/cursor (``seq``) exactly like the
  run-event stream; a sink failure never rolls back the persisted notice —
  live events are best-effort, the inbox is the durable record.
- **C03 credential-reference-only outbound** — the only outbound transport
  in v1 is :class:`WebhookChannel`, and it holds a credential *reference*
  (a resolver name, C03 rule 6), never a URL: the URL is resolved at
  delivery time through the injected resolver, used for one bounded POST,
  and never persisted, logged or embedded in an error. There is no
  messenger integration (C10 rule 5: ``NotificationChannel`` is the seam;
  adapters are future cards).

**Severity provenance on stored rows** — ``notification_alerts`` has no
severity column (frozen schema); a notice's severity lives at its anchor.
While the anchor exists (``finding_id`` set, finding not yet GC-swept) the
severity is read by join. But anchors are legally severed (ON DELETE SET
NULL by the retention sweep / run GC), and job-result notices have no
finding anchor at all — so every draft this module writes appends a frozen
machine tag ``[severity=<v>]`` to the bounded summary. Writer and reader
of that format both live in this module; :func:`_row_severity` prefers the
live anchor join and falls back to the tag, so quiet-hours/routing
decisions stay correct even for severed anchors. This is a documented
self-consistency contract of the sole writer of these rows, pinned by the
focused tests.

**LLM independence (brief acceptance 2)** — the module imports no agent,
model or durable-engine symbol: deterministic collectors and notifications
keep working with the model endpoint unreachable (C05 rule 8, C10 rule 4).
The structural import audit is pinned by the focused tests.

Retention: every inserted notice carries ``retain_until = now +
:data:`RETENTION_HORIZON`` (NOT NULL, indexed — brief acceptance 1). The
sweep itself (``DELETE ... WHERE retain_until < now()``) is owned by the
scheduled collectors card; this module only bounds each row it writes.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Iterable, Literal, Mapping, Protocol, Sequence, runtime_checkable

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.observations import (
    CONTEXT_REF_MAX_CHARS,
    DeliveryStatus,
    FindingSeverity,
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    validate_alert_shape,
)

__all__ = [
    "DEFAULT_COOLDOWN",
    "MAX_FINDINGS_PER_ROUND",
    "MAX_PENDING_PER_SWEEP",
    "MAX_WEBHOOK_BODY_BYTES",
    "MAX_WEBHOOK_RESPONSE_BYTES",
    "RETENTION_HORIZON",
    "WEBHOOK_TIMEOUT_SECONDS",
    "WEBHOOK_USER_AGENT",
    "AlertDraft",
    "ChannelDeliveryError",
    "CredentialResolver",
    "DeliveryPayload",
    "EventSink",
    "NotificationChannel",
    "NotificationError",
    "NotificationErrorCode",
    "NotificationEvent",
    "NoticeOutcome",
    "QuietHours",
    "RoutingPolicy",
    "SweepResult",
    "WebhookChannel",
    "deliver_pending",
    "envelope_ts",
    "evaluate_findings",
    "finding_alert_draft",
    "finding_dedup_key",
    "in_quiet_hours",
    "job_result_alert_draft",
    "notify_job_result",
]

#: Retention bound stamped on every notice this module inserts (NOT NULL,
#: indexed). Alerts are ack/audit evidence and outlive the collectors' 7-day
#: sample horizon; the sweep itself is the scheduled collectors card's job.
RETENTION_HORIZON = timedelta(days=30)

#: Default re-delivery cooldown for a still-firing condition (the
#: alertmanager ``repeat_interval`` analog). Policy-overridable.
DEFAULT_COOLDOWN = timedelta(hours=4)

#: Bounded evaluation rounds: one round evaluates at most this many open
#: findings, most severe / oldest first. A backlog beyond the cap is picked
#: up by the next round — never an unbounded transaction.
MAX_FINDINGS_PER_ROUND = 50

#: Bounded pending-delivery sweep per server per round.
MAX_PENDING_PER_SWEEP = 50

#: Outbound webhook budget: bounded JSON body, bounded response drain,
#: bounded wall clock (the sibling-collector budget discipline).
MAX_WEBHOOK_BODY_BYTES = 8 * 1024
MAX_WEBHOOK_RESPONSE_BYTES = 8 * 1024
WEBHOOK_TIMEOUT_SECONDS = 10.0
WEBHOOK_USER_AGENT = "mai-notifications/1"

#: Severity attention order (drives escalation detection and evaluation
#: ordering). Keys mirror :class:`mai.contracts.observations.FindingSeverity`.
_SEVERITY_RANK: dict[str, int] = {
    FindingSeverity.INFO.value: 0,
    FindingSeverity.WARNING.value: 1,
    FindingSeverity.CRITICAL.value: 2,
}

#: The one job-result notice vocabulary (gate scenario: completed/failed
#: fixtures). Success is a notice too — an operator sees the run ended.
#: ``critical`` is reserved for conditions (security findings), never an
#: outcome label.
_JOB_OUTCOME_SEVERITY: dict[str, str] = {
    "succeeded": FindingSeverity.INFO.value,
    "failed": FindingSeverity.WARNING.value,
}

#: Frozen machine tag appended to every draft summary (see module docstring
#: "Severity provenance on stored rows"). Writer and reader both live here.
_SEVERITY_TAG_RE = re.compile(r"\[severity=(info|warning|critical)\]$")

JobOutcome = Literal["succeeded", "failed"]


class NotificationErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_POLICY = "invalid_policy"
    UNKNOWN_CHANNEL = "unknown_channel"
    INVALID_DRAFT = "invalid_draft"
    INVALID_EVENT = "invalid_event"


class NotificationError(ValueError):
    """One structured, content-free service rejection."""

    def __init__(self, code: NotificationErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Quiet hours (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuietHours:
    """Daily quiet window for OUTBOUND delivery (web inbox always visible).

    ``start``/``end`` are local wall-clock ``time`` values in ``tzinfo``;
    the window may cross midnight (``start > end``). An "always quiet" or
    "never quiet" window is not expressible — pass ``quiet_hours=None`` in
    the policy instead of a degenerate window.
    """

    start: time
    end: time
    tzinfo: timezone

    def __post_init__(self) -> None:
        if not isinstance(self.start, time) or not isinstance(self.end, time):
            raise NotificationError(
                NotificationErrorCode.INVALID_POLICY,
                "quiet hours bounds must be datetime.time values",
            )
        if self.start == self.end:
            raise NotificationError(
                NotificationErrorCode.INVALID_POLICY,
                "quiet hours bounds must differ (a degenerate window is the "
                "absence of a QuietHours object)",
            )


def in_quiet_hours(at: datetime, window: QuietHours) -> bool:
    """Is ``at`` inside the daily quiet window (pure)?

    Naive timestamps are rejected: delivery decisions are made against
    timezone-aware UTC instants (C01 rule 5), never a server-local guess.
    """
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    local = at.astimezone(window.tzinfo).time()
    if window.start < window.end:
        return window.start <= local < window.end
    # Window crosses midnight: quiet is [start .. 24:00) ∪ [00:00 .. end).
    return local >= window.start or local < window.end


# ---------------------------------------------------------------------------
# Routing policy (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Frozen notification routing for one evaluation/sweep call.

    ``channels_by_severity`` maps each of the three frozen severities to a
    tuple of outbound channel names (possibly empty = inbox-only). Unknown
    severity keys and non-positive cooldowns are rejected at construction;
    unknown channel NAMES are rejected when the policy is resolved against
    the caller's channel registry (see :func:`_resolve_channels`), because
    the name space is owned by the call site, not the policy.
    """

    cooldown: timedelta = DEFAULT_COOLDOWN
    quiet_hours: QuietHours | None = None
    channels_by_severity: Mapping[str, Sequence[str]] = field(
        default_factory=lambda: {
            FindingSeverity.INFO.value: (),
            FindingSeverity.WARNING.value: (),
            FindingSeverity.CRITICAL.value: (),
        }
    )

    def __post_init__(self) -> None:
        if not isinstance(self.cooldown, timedelta) or self.cooldown <= timedelta(0):
            raise NotificationError(
                NotificationErrorCode.INVALID_POLICY,
                "cooldown must be a positive timedelta",
            )
        unknown = sorted(set(self.channels_by_severity) - set(_SEVERITY_RANK))
        if unknown:
            raise NotificationError(
                NotificationErrorCode.INVALID_POLICY,
                f"unknown severity keys in routing policy: {', '.join(unknown)}",
            )
        for severity, names in self.channels_by_severity.items():
            for name in names:
                if not isinstance(name, str) or not name:
                    raise NotificationError(
                        NotificationErrorCode.INVALID_POLICY,
                        f"channel names for {severity!r} must be non-empty strings",
                    )
        if self.quiet_hours is not None and not isinstance(
            self.quiet_hours, QuietHours
        ):
            raise NotificationError(
                NotificationErrorCode.INVALID_POLICY,
                "quiet_hours must be a QuietHours instance",
            )

    def channels_for(self, severity: str) -> tuple[str, ...]:
        """Channel names routed for one severity (unknown severity = empty)."""
        return tuple(self.channels_by_severity.get(severity, ()))


def _resolve_channels(
    policy: RoutingPolicy, channels: Mapping[str, "NotificationChannel"]
) -> None:
    """Fail closed on policy routes naming channels the call site lacks."""
    for severity, names in policy.channels_by_severity.items():
        for name in names:
            if name not in channels:
                raise NotificationError(
                    NotificationErrorCode.UNKNOWN_CHANNEL,
                    f"policy routes severity {severity!r} to unknown channel {name!r}",
                )


# ---------------------------------------------------------------------------
# Event seam (frozen C05 rule 4 envelope shape)
# ---------------------------------------------------------------------------


def envelope_ts(ts: datetime) -> str:
    """Render one envelope timestamp (RFC3339 UTC, explicit ``Z`` offset).

    Naive timestamps are rejected — the envelope never carries a local-time
    guess (C01 rule 5).
    """
    if ts.tzinfo is None:
        raise ValueError("ts must be timezone-aware")
    return (
        ts.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """One notification event awaiting its stream cursor (C05 rule 4).

    The service composes everything except ``seq``: the cursor belongs to
    the durable stream the sink writes (the run-event stream allocates
    ``MAX(seq)+1`` under the run lock — the sink's ownership, not this
    module's). ``run_id`` is the notice's run anchor, ``None`` for finding
    notices; the frozen field set is always emitted so consumers can share
    one parser across run and notification streams.
    """

    type: Literal[
        "notification.created",
        "notification.delivered",
        "notification.failed",
    ]
    ts: datetime
    run_id: uuid.UUID | None
    data: Mapping[str, object]

    def envelope(self, seq: int) -> dict[str, object]:
        """The frozen wire shape: ``{"seq", "run_id", "ts", "type", "v", "data"}``."""
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise NotificationError(
                NotificationErrorCode.INVALID_EVENT,
                "seq must be an integer >= 1 (the stream cursor allocates it)",
            )
        return {
            "seq": seq,
            "run_id": str(self.run_id) if self.run_id is not None else None,
            "ts": envelope_ts(self.ts),
            "type": self.type,
            "v": 1,
            "data": dict(self.data),
        }


@runtime_checkable
class EventSink(Protocol):
    """Where live notification events go (the SSE seam).

    Implementations own cursor allocation and transport (the production
    sink appends to the durable event stream behind the SSE endpoints); the
    service awaits :meth:`emit` once per event. Sink failures never roll
    back the durable notice.
    """

    async def emit(self, event: NotificationEvent) -> int: ...


# ---------------------------------------------------------------------------
# Drafts (pure): condition → bounded notice content
# ---------------------------------------------------------------------------

#: Dedup keys are hashes of the condition identity, so the bounded 256-char
#: column can never truncate two distinct conditions onto one key: a finding
#: kind (≤ 64) plus its collector dedup key (≤ 256) exceed the notice bound
#: together, and silent truncation would merge distinct conditions.
#: ``finding:<32 hex>`` is stable, bounded and collision-free.
_FINDING_DEDUP_PREFIX = "finding:"


def finding_dedup_key(kind: str, dedup_key: str) -> str:
    """Deterministic notice dedup key for one finding condition (pure)."""
    digest = hashlib.sha256(f"{kind}\x1f{dedup_key}".encode()).hexdigest()[:32]
    return _FINDING_DEDUP_PREFIX + digest


def job_result_dedup_key(run_id: uuid.UUID) -> str:
    """Deterministic notice dedup key for one durable run (pure)."""
    return f"job_result:{run_id}"


def _severity_tagged(summary: str, severity: str) -> str:
    """Summary + frozen machine tag, within the column bound (pure)."""
    tag = f" [severity={severity}]"
    return _truncate(summary, SUMMARY_MAX_CHARS - len(tag)) + tag


@dataclass(frozen=True, slots=True)
class AlertDraft:
    """One evaluated condition, ready to persist (bounded text only).

    Every text field is within its 0007 column bound — the draft builder
    truncates, so an oversized collector summary becomes a bounded notice,
    never an integrity failure.
    """

    origin: str  # frozen AlertOrigin vocabulary
    dedup_key: str
    severity: str  # frozen FindingSeverity vocabulary
    title: str
    summary: str
    context_ref: str | None
    run_id: uuid.UUID | None = None
    finding_id: uuid.UUID | None = None


def finding_alert_draft(row: Mapping[str, object]) -> AlertDraft:
    """Build the notice draft for one open finding row (pure).

    ``severity`` passes through from the finding (the collector owns it);
    ``context_ref`` is the finding reference usable in chat (monitor-api),
    never a payload.
    """
    finding_id = row["finding_id"]
    kind = row["kind"]
    condition_key = row["dedup_key"]
    if not isinstance(finding_id, uuid.UUID) or not isinstance(kind, str):
        raise NotificationError(
            NotificationErrorCode.INVALID_DRAFT,
            "finding row must carry a UUID finding_id and a kind",
        )
    severity = str(row.get("severity") or FindingSeverity.INFO.value)
    if severity not in _SEVERITY_RANK:
        raise NotificationError(
            NotificationErrorCode.INVALID_DRAFT,
            f"finding carries unknown severity {severity!r}",
        )
    summary = str(row.get("summary") or "")
    return AlertDraft(
        origin="finding",
        dedup_key=finding_dedup_key(kind, str(condition_key)),
        severity=severity,
        title=_truncate(f"{kind}: {summary}", TITLE_MAX_CHARS),
        summary=_severity_tagged(summary, severity),
        context_ref=_truncate(f"finding:{finding_id}", CONTEXT_REF_MAX_CHARS),
        finding_id=finding_id,
    )


def job_result_alert_draft(
    run_id: uuid.UUID,
    outcome: JobOutcome,
    *,
    model_profile: str,
    error_ref: str | None = None,
) -> AlertDraft:
    """Build the notice draft for one finished durable run (pure).

    Completed AND failed runs notify (the gate scenario's completed/failed
    fixtures): success is ``info``, failure ``warning``. ``error_ref`` is
    the run's bounded error REFERENCE (C05 rule 3) — a pointer, never an
    error payload.
    """
    severity = _JOB_OUTCOME_SEVERITY.get(outcome)
    if severity is None:
        raise NotificationError(
            NotificationErrorCode.INVALID_DRAFT,
            f"unknown job outcome {outcome!r} (expected succeeded|failed)",
        )
    short = str(run_id)[:8]
    title = _truncate(f"Run {short} {outcome}", TITLE_MAX_CHARS)
    summary = _severity_tagged(
        f"model_profile={model_profile} outcome={outcome}"
        + (f" error_ref={error_ref}" if error_ref else ""),
        severity,
    )
    return AlertDraft(
        origin="job_result",
        dedup_key=job_result_dedup_key(run_id),
        severity=severity,
        title=title,
        summary=summary,
        context_ref=_truncate(f"run:{run_id}", CONTEXT_REF_MAX_CHARS),
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Channels (C03 credential-reference-only outbound)
# ---------------------------------------------------------------------------


@runtime_checkable
class CredentialResolver(Protocol):
    """The C03 rule 6 resolver seam (``EnvResolver`` satisfies it).

    ``resolve`` takes a credential NAME and returns secret material; the
    value's lifetime is one delivery — the service never stores it.
    """

    def resolve(self, name: str):  # -> SecretValue (structural, untyped here)
        ...


@dataclass(frozen=True, slots=True)
class DeliveryPayload:
    """The bounded outbound rendering of one notice (no credential material)."""

    alert_id: uuid.UUID
    server_id: uuid.UUID
    origin: str
    dedup_key: str
    severity: str
    title: str
    summary: str | None
    context_ref: str | None
    created_at: datetime
    event_type: str
    decided_at: datetime

    def to_json_dict(self) -> dict[str, object]:
        """The POST body object — bounded references and human text only."""
        return {
            "alert_id": str(self.alert_id),
            "server_id": str(self.server_id),
            "origin": self.origin,
            "dedup_key": self.dedup_key,
            "severity": self.severity,
            "title": self.title,
            "summary": self.summary,
            "context_ref": self.context_ref,
            "created_at": envelope_ts(self.created_at),
            "event": self.event_type,
            "ts": envelope_ts(self.decided_at),
        }


class ChannelDeliveryError(RuntimeError):
    """One failed outbound delivery — value-free and URL-free by contract.

    The webhook URL is credential material (it embeds a token on every
    hosted messenger-style endpoint); neither the URL nor the resolved
    secret ever appears in ``code``, ``detail`` or ``str()``. The detail
    carries only the channel NAME (a config reference) and a stable code.
    """

    def __init__(self, channel: str, code: str) -> None:
        self.channel = channel
        self.code = code
        super().__init__(f"{channel}: {code}")


@runtime_checkable
class NotificationChannel(Protocol):
    """The C10 rule 5 delivery seam (messenger adapters are future cards)."""

    name: str

    async def deliver(self, payload: DeliveryPayload) -> None:
        """Deliver one notice or raise :class:`ChannelDeliveryError`."""
        ...


class WebhookChannel:
    """Outbound JSON webhook; the URL resolves by credential REFERENCE.

    C03 rule 6: credentials resolve by reference at use time. The channel
    object holds the resolver NAME (``credential_ref``), never the URL;
    :meth:`deliver` resolves the URL for one bounded POST and discards it.
    The POST carries no Authorization header (the URL IS the credential on
    typical receivers), never follows redirects (a redirect would leak the
    credential-bearing URL to a third party), and drains the response under
    a byte cap.
    """

    def __init__(
        self,
        name: str,
        credential_ref: str,
        *,
        credentials: CredentialResolver,
        timeout: float = WEBHOOK_TIMEOUT_SECONDS,
        transport: object | None = None,
    ) -> None:
        if not name or not credential_ref:
            raise NotificationError(
                NotificationErrorCode.INVALID_POLICY,
                "webhook channel needs a name and a credential reference",
            )
        self.name = name
        self.credential_ref = credential_ref
        self._credentials = credentials
        self._timeout = timeout
        self._transport = transport

    async def deliver(self, payload: DeliveryPayload) -> None:
        """One bounded POST; every failure is a value-free channel error."""
        import httpx2

        try:
            secret = self._credentials.resolve(self.credential_ref)
        except Exception:
            # The first-party resolver's rejections are value-free
            # (EnvFileError); any other resolver is the call site's choice —
            # same discipline. No secret/URL material is re-raised.
            raise ChannelDeliveryError(self.name, "credential_unresolved") from None
        url = secret.value
        body = json.dumps(payload.to_json_dict()).encode()
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            raise ChannelDeliveryError(self.name, "oversized_body")
        try:
            async with httpx2.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,  # type: ignore[arg-type]
                follow_redirects=False,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": WEBHOOK_USER_AGENT,
                },
            ) as client:
                # Streamed like the sibling collectors: the status gates the
                # read, and the body is drained under an early-abort cap so
                # a hostile receiver cannot stream unbounded bytes into the
                # controller. Redirects are NOT followed (a 3xx would leak
                # the credential-bearing URL to a third party) and any
                # non-2xx answer is a failed attempt.
                async with client.stream("POST", url, content=body) as response:
                    if response.status_code // 100 != 2:
                        raise ChannelDeliveryError(
                            self.name, f"http_{response.status_code}"
                        )
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_WEBHOOK_RESPONSE_BYTES:
                            raise ChannelDeliveryError(self.name, "oversized_response")
        except ChannelDeliveryError:
            raise
        except httpx2.TimeoutException:
            raise ChannelDeliveryError(self.name, "timeout") from None
        except httpx2.TransportError:
            raise ChannelDeliveryError(self.name, "unreachable") from None
        # Any other exception type is a programming error in this module's
        # own code — it must surface, not be laundered into a channel code.


# ---------------------------------------------------------------------------
# Persistence + delivery pipeline (durable, DB-backed)
# ---------------------------------------------------------------------------

_INSERT_ALERT = sa.text(
    """
    INSERT INTO notification_alerts
        (alert_id, server_id, origin, run_id, finding_id, dedup_key, title,
         summary, context_ref, delivery_status, created_at, delivered_at,
         acknowledged_at, retain_until)
    VALUES
        (:alert_id, :server_id, :origin, :run_id, :finding_id, :dedup_key,
         :title, :summary, :context_ref, 'pending', :created_at, NULL,
         NULL, :retain_until)
    ON CONFLICT (server_id, dedup_key) DO NOTHING
    RETURNING alert_id
    """
)

_LOAD_ALERT_BY_KEY = sa.text(
    """
    SELECT alert_id, origin, run_id, finding_id, dedup_key, title, summary,
           context_ref, delivery_status, created_at, delivered_at,
           acknowledged_at
      FROM notification_alerts
     WHERE server_id = :server_id AND dedup_key = :dedup_key
    """
)

_UPDATE_ALERT_CONTENT = sa.text(
    """
    UPDATE notification_alerts
       SET title = :title, summary = :summary, context_ref = :context_ref,
           finding_id = :finding_id, run_id = :run_id
     WHERE alert_id = :alert_id
    """
)

_SET_DELIVERY = sa.text(
    """
    UPDATE notification_alerts
       SET delivery_status = :status, delivered_at = :delivered_at
     WHERE alert_id = :alert_id
    """
)

_PENDING_ALERTS = sa.text(
    """
    SELECT alert_id, origin, run_id, finding_id, dedup_key, title, summary,
           context_ref, delivery_status, created_at, anchor_severity
      FROM (
        SELECT a.alert_id, a.origin, a.run_id, a.finding_id, a.dedup_key,
               a.title, a.summary, a.context_ref, a.delivery_status,
               a.created_at, f.severity AS anchor_severity,
               ROW_NUMBER() OVER (
                   ORDER BY a.created_at, a.alert_id
               ) AS rn
          FROM notification_alerts a
          LEFT JOIN observation_findings f ON f.finding_id = a.finding_id
         WHERE a.server_id = :server_id AND a.delivery_status = 'pending'
       ) ranked
     WHERE rn <= :cap
    """
)

_OPEN_FINDINGS = sa.text(
    """
    SELECT finding_id, kind, dedup_key, severity, summary, detail, opened_at
      FROM (
        SELECT f.finding_id, f.kind, f.dedup_key, f.severity, f.summary,
               f.detail, f.opened_at,
               ROW_NUMBER() OVER (
                   ORDER BY CASE f.severity
                       WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2
                   END, f.opened_at, f.finding_id
               ) AS rn
          FROM observation_findings f
         WHERE f.server_id = :server_id AND f.resolved_at IS NULL
       ) ranked
     WHERE rn <= :cap
    """
)


def _utc(value: datetime) -> datetime:
    """Normalize one aware timestamp to UTC (C01 rule 5)."""
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _row_severity(row: Mapping[str, object]) -> str:
    """Severity of a stored notice row for DELIVERY routing (see module docstring).

    Live anchor join first (the collector's freshest severity); the frozen
    summary tag covers severed anchors and job-result notices; ``info`` only
    if both are gone (not produced by this module's drafts — the tag is
    always written).
    """
    anchor = row.get("anchor_severity")
    if isinstance(anchor, str) and anchor in _SEVERITY_RANK:
        return anchor
    return _stored_severity(row)


def _stored_severity(row: Mapping[str, object]) -> str:
    """The severity AS LAST PERSISTED on the notice row (tag-based).

    Escalation detection compares the condition's current severity against
    this — the live anchor join is already the NEW severity by the time the
    evaluator runs, so the durable tag is the only honest "what did we last
    tell the human" reference.
    """
    summary = row.get("summary")
    if isinstance(summary, str):
        match = _SEVERITY_TAG_RE.search(summary)
        if match is not None:
            return match.group(1)
    anchor = row.get("severity")
    if isinstance(anchor, str) and anchor in _SEVERITY_RANK:
        return anchor
    return FindingSeverity.INFO.value


async def _emit(
    sink: EventSink | None,
    *,
    event_type: str,
    ts: datetime,
    row: Mapping[str, object],
    server_id: uuid.UUID,
    severity: str,
) -> str | None:
    """Emit one live event; a sink failure is captured, never fatal."""
    if sink is None:
        return None
    run_id = row.get("run_id")
    event = NotificationEvent(
        type=event_type,  # type: ignore[arg-type]
        ts=ts,
        run_id=run_id if isinstance(run_id, uuid.UUID) else None,
        data={
            "alert_id": str(row["alert_id"]),
            "server_id": str(server_id),
            "dedup_key": str(row["dedup_key"]),
            "severity": severity,
            "origin": str(row["origin"]),
        },
    )
    try:
        await sink.emit(event)
    except Exception as error:  # noqa: BLE001 - the sink is a seam
        return type(error).__name__
    return None


@dataclass(frozen=True, slots=True)
class NoticeOutcome:
    """What one evaluation decided about one condition.

    ``disposition``: ``created`` | ``deduped`` | ``escalated`` |
    ``rescored`` | ``reminder`` | ``unchanged`` (acknowledged).
    ``delivery``: ``delivered`` | ``pending`` (quiet-hours deferral) |
    ``failed`` | ``skipped`` (no outbound round ran).
    """

    alert_id: uuid.UUID
    dedup_key: str
    disposition: str
    delivery: str
    channels_attempted: tuple[str, ...] = ()
    event_error: str | None = None


def _payload_of(
    row: Mapping[str, object],
    server_id: uuid.UUID,
    *,
    severity: str,
    event_type: str,
    decided_at: datetime,
) -> DeliveryPayload:
    summary = row.get("summary")
    context_ref = row.get("context_ref")
    return DeliveryPayload(
        alert_id=row["alert_id"],  # type: ignore[arg-type]
        server_id=server_id,
        origin=str(row["origin"]),
        dedup_key=str(row["dedup_key"]),
        severity=severity,
        title=str(row["title"]),
        summary=str(summary) if summary else None,
        context_ref=str(context_ref) if context_ref else None,
        created_at=row["created_at"],  # type: ignore[arg-type]
        event_type=event_type,
        decided_at=decided_at,
    )


async def _stamp(
    sessions: async_sessionmaker[AsyncSession],
    alert_id: object,
    *,
    status: DeliveryStatus,
    delivered_at: datetime | None,
) -> None:
    """One durable lifecycle transition (own committed transaction)."""
    assert (status is DeliveryStatus.DELIVERED) == (delivered_at is not None)
    async with sessions() as session:
        await session.execute(
            _SET_DELIVERY,
            {
                "status": status.value,
                "delivered_at": delivered_at,
                "alert_id": alert_id,
            },
        )
        await session.commit()


async def _attempt_outbound(
    row: Mapping[str, object],
    server_id: uuid.UUID,
    *,
    routes: tuple[str, ...],
    channels: Mapping[str, NotificationChannel],
    decided_at: datetime,
) -> tuple[bool, tuple[str, ...]]:
    """Run the outbound round; return (all_ok, attempted_names).

    Every routed channel is attempted even after one fails (no channel can
    suppress its siblings); the row's status reports the honest aggregate.
    """
    attempted: list[str] = []
    ok = True
    for name in routes:
        attempted.append(name)
        try:
            await channels[name].deliver(
                _payload_of(
                    row,
                    server_id,
                    severity=_row_severity(row),
                    event_type="notification.delivered",
                    decided_at=decided_at,
                )
            )
        except ChannelDeliveryError:
            ok = False
    return ok, tuple(attempted)


async def _deliver_row(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    row: Mapping[str, object],
    *,
    now: datetime,
    policy: RoutingPolicy,
    channels: Mapping[str, NotificationChannel],
    sink: EventSink | None,
    bypass_quiet_hours: bool = False,
    emit_created: bool = False,
) -> NoticeOutcome:
    """Route one persisted notice row through the delivery decision tree.

    The row is already durable; this only decides and records HOW it
    reaches its routes. Every terminal transition is one committed UPDATE.
    ``emit_created`` is set exactly once per notice lifetime (creation).
    """
    severity = _row_severity(row)
    routes = policy.channels_for(severity)
    deferred = (
        bool(routes)
        and not bypass_quiet_hours
        and policy.quiet_hours is not None
        and in_quiet_hours(now, policy.quiet_hours)
        and severity != FindingSeverity.CRITICAL.value
    )
    if deferred:
        # Stay pending: the sweep flushes it after the window.
        event_error = await _emit(
            sink,
            event_type="notification.created",
            ts=now,
            row=row,
            server_id=server_id,
            severity=severity,
        )
        return NoticeOutcome(
            alert_id=row["alert_id"],  # type: ignore[arg-type]
            dedup_key=str(row["dedup_key"]),
            disposition="created" if emit_created else "deferred",
            delivery="pending",
            event_error=event_error,
        )
    event_error: str | None = None
    if emit_created:
        event_error = await _emit(
            sink,
            event_type="notification.created",
            ts=now,
            row=row,
            server_id=server_id,
            severity=severity,
        )
    if not routes:
        # Inbox-only: delivered the moment the row exists (inbox presence
        # IS the row). Terminal, stamped.
        await _stamp(
            sessions,
            row["alert_id"],
            status=DeliveryStatus.DELIVERED,
            delivered_at=now,
        )
        delivered_error = await _emit(
            sink,
            event_type="notification.delivered",
            ts=now,
            row=row,
            server_id=server_id,
            severity=severity,
        )
        return NoticeOutcome(
            alert_id=row["alert_id"],  # type: ignore[arg-type]
            dedup_key=str(row["dedup_key"]),
            disposition="created" if emit_created else "deferred",
            delivery="delivered",
            event_error=event_error or delivered_error,
        )
    ok, attempted = await _attempt_outbound(
        row, server_id, routes=routes, channels=channels, decided_at=now
    )
    status = DeliveryStatus.DELIVERED if ok else DeliveryStatus.FAILED
    await _stamp(
        sessions,
        row["alert_id"],
        status=status,
        delivered_at=now if ok else None,
    )
    delivered_error = await _emit(
        sink,
        event_type="notification.delivered" if ok else "notification.failed",
        ts=now,
        row=row,
        server_id=server_id,
        severity=severity,
    )
    return NoticeOutcome(
        alert_id=row["alert_id"],  # type: ignore[arg-type]
        dedup_key=str(row["dedup_key"]),
        disposition="created" if emit_created else "deferred",
        delivery=status.value,
        channels_attempted=attempted,
        event_error=event_error or delivered_error,
    )


async def _persist_new(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    draft: AlertDraft,
    *,
    now: datetime,
) -> uuid.UUID | None:
    """Insert the notice; ``None`` when the condition already has one."""
    validate_alert_shape(draft.origin, draft.run_id, draft.finding_id)
    async with sessions() as session:
        inserted = (
            await session.execute(
                _INSERT_ALERT,
                {
                    "alert_id": uuid.uuid4(),
                    "server_id": server_id,
                    "origin": draft.origin,
                    "run_id": draft.run_id,
                    "finding_id": draft.finding_id,
                    "dedup_key": draft.dedup_key,
                    "title": draft.title,
                    "summary": draft.summary,
                    "context_ref": draft.context_ref,
                    "created_at": now,
                    "retain_until": now + RETENTION_HORIZON,
                },
            )
        ).first()
        await session.commit()
    return inserted[0] if inserted else None


async def _evaluate_one(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    draft: AlertDraft,
    *,
    now: datetime,
    policy: RoutingPolicy,
    channels: Mapping[str, NotificationChannel],
    sink: EventSink | None,
) -> NoticeOutcome:
    """Dedup → cooldown → escalation → delivery decision for one draft."""
    alert_id = await _persist_new(sessions, server_id, draft, now=now)
    if alert_id is not None:
        row = {
            "alert_id": alert_id,
            "run_id": draft.run_id,
            "origin": draft.origin,
            "dedup_key": draft.dedup_key,
            "title": draft.title,
            "summary": draft.summary,
            "context_ref": draft.context_ref,
            "created_at": now,
            "anchor_severity": None,  # route by the freshly written tag
        }
        return await _deliver_row(
            sessions,
            server_id,
            row,
            now=now,
            policy=policy,
            channels=channels,
            sink=sink,
            emit_created=True,
        )

    # Existing notice: dedup / cooldown / escalation / ack decision.
    async with sessions() as session:
        existing = (
            (
                await session.execute(
                    _LOAD_ALERT_BY_KEY,
                    {"server_id": server_id, "dedup_key": draft.dedup_key},
                )
            )
            .mappings()
            .first()
        )
        await session.commit()
    if existing is None:  # unreachable: the UNIQUE row raced away
        raise NotificationError(
            NotificationErrorCode.INVALID_DRAFT,
            "existing notice vanished between insert and load",
        )
    if existing["acknowledged_at"] is not None:
        return NoticeOutcome(
            alert_id=existing["alert_id"],  # type: ignore[arg-type]
            dedup_key=draft.dedup_key,
            disposition="unchanged",
            delivery="skipped",
        )

    stored_severity = _stored_severity(existing)

    if stored_severity != draft.severity:
        escalated = _SEVERITY_RANK.get(draft.severity, 0) > _SEVERITY_RANK.get(
            stored_severity, 0
        )
        async with sessions() as session:
            await session.execute(
                _UPDATE_ALERT_CONTENT,
                {
                    "title": draft.title,
                    "summary": draft.summary,
                    "context_ref": draft.context_ref,
                    "finding_id": draft.finding_id,
                    "run_id": draft.run_id,
                    "alert_id": existing["alert_id"],
                },
            )
            await session.commit()
        if not escalated:
            # De-escalation: content refresh only, never a delivery — only
            # escalations interrupt a human.
            return NoticeOutcome(
                alert_id=existing["alert_id"],  # type: ignore[arg-type]
                dedup_key=draft.dedup_key,
                disposition="rescored",
                delivery="skipped",
            )
        row = dict(existing)
        row["anchor_severity"] = None  # force the refreshed draft's tag
        row["summary"] = draft.summary
        outcome = await _deliver_row(
            sessions,
            server_id,
            row,
            now=now,
            policy=policy,
            channels=channels,
            sink=sink,
            bypass_quiet_hours=draft.severity == FindingSeverity.CRITICAL.value,
        )
        return NoticeOutcome(
            alert_id=outcome.alert_id,
            dedup_key=outcome.dedup_key,
            disposition="escalated",
            delivery=outcome.delivery,
            channels_attempted=outcome.channels_attempted,
            event_error=outcome.event_error,
        )

    delivered_at = existing["delivered_at"]
    if existing["delivery_status"] == DeliveryStatus.DELIVERED.value:
        if delivered_at is None:  # CHECK-pinned equivalence; unreachable
            raise NotificationError(
                NotificationErrorCode.INVALID_DRAFT,
                "delivered notice without its stamp violates the schema",
            )
        if now - _utc(delivered_at) < policy.cooldown:
            return NoticeOutcome(
                alert_id=existing["alert_id"],  # type: ignore[arg-type]
                dedup_key=draft.dedup_key,
                disposition="deduped",
                delivery="skipped",
            )
        outcome = await _deliver_row(
            sessions,
            server_id,
            dict(existing),
            now=now,
            policy=policy,
            channels=channels,
            sink=sink,
        )
        return NoticeOutcome(
            alert_id=outcome.alert_id,
            dedup_key=outcome.dedup_key,
            disposition="reminder",
            delivery=outcome.delivery,
            channels_attempted=outcome.channels_attempted,
            event_error=outcome.event_error,
        )

    # pending / failed existing notice: the sweep owns retries (honest
    # durable state — no failure timestamps exist to cooldown against).
    return NoticeOutcome(
        alert_id=existing["alert_id"],  # type: ignore[arg-type]
        dedup_key=draft.dedup_key,
        disposition="deduped",
        delivery="skipped",
    )


async def evaluate_findings(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    *,
    now: datetime,
    policy: RoutingPolicy,
    channels: Mapping[str, NotificationChannel] | None = None,
    sink: EventSink | None = None,
) -> tuple[NoticeOutcome, ...]:
    """Evaluate one server's open findings into inbox notices.

    Bounded round (at most :data:`MAX_FINDINGS_PER_ROUND` open findings,
    most severe / oldest first). Repeated polls of the same condition
    dedup onto the existing notice: inside the cooldown a delivered,
    unacknowledged notice is a no-op; after it, a repeat reminder; an
    escalation re-delivers immediately; an acknowledged notice is never
    nagged. Deterministic and LLM-free (brief acceptance 2).
    """
    now = _utc(now)
    registry = channels if channels is not None else {}
    _resolve_channels(policy, registry)
    async with sessions() as session:
        rows = (
            (
                await session.execute(
                    _OPEN_FINDINGS,
                    {"server_id": server_id, "cap": MAX_FINDINGS_PER_ROUND},
                )
            )
            .mappings()
            .all()
        )
        await session.commit()
    return tuple(
        await _gather(
            sessions,
            server_id,
            (finding_alert_draft(row) for row in rows),
            now=now,
            policy=policy,
            channels=registry,
            sink=sink,
        )
    )


async def _gather(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    drafts: Iterable[AlertDraft],
    *,
    now: datetime,
    policy: RoutingPolicy,
    channels: Mapping[str, NotificationChannel],
    sink: EventSink | None,
) -> list[NoticeOutcome]:
    outcomes: list[NoticeOutcome] = []
    for draft in drafts:
        outcomes.append(
            await _evaluate_one(
                sessions,
                server_id,
                draft,
                now=now,
                policy=policy,
                channels=channels,
                sink=sink,
            )
        )
    return outcomes


async def notify_job_result(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    run_id: uuid.UUID,
    outcome: JobOutcome,
    *,
    model_profile: str,
    error_ref: str | None = None,
    now: datetime,
    policy: RoutingPolicy,
    channels: Mapping[str, NotificationChannel] | None = None,
    sink: EventSink | None = None,
) -> NoticeOutcome:
    """Persist + deliver one job-result notice (completed/failed fixtures).

    The run's FK is the storage boundary: an unknown ``run_id`` fails closed
    before any insert, and a run anchored to a DIFFERENT server is refused
    (C01 rule 7 — the notice is written under the run's own server, never
    re-attributed). A run notifies exactly once (the dedup key is the run
    id); a duplicate call is a recorded no-op.
    """
    now = _utc(now)
    registry = channels if channels is not None else {}
    _resolve_channels(policy, registry)
    async with sessions() as session:
        run_server = (
            await session.execute(
                sa.text("SELECT server_id FROM job_runs WHERE run_id = :r"),
                {"r": run_id},
            )
        ).scalar()
        await session.commit()
    if run_server is None:
        raise NotificationError(
            NotificationErrorCode.INVALID_DRAFT,
            "no job_runs row for this run_id (storage boundary is the FK)",
        )
    if uuid.UUID(str(run_server)) != server_id:
        raise NotificationError(
            NotificationErrorCode.INVALID_DRAFT,
            "run belongs to a different server (C01 rule 7 scope boundary)",
        )
    draft = job_result_alert_draft(
        run_id, outcome, model_profile=model_profile, error_ref=error_ref
    )
    return await _evaluate_one(
        sessions,
        server_id,
        draft,
        now=now,
        policy=policy,
        channels=registry,
        sink=sink,
    )


@dataclass(frozen=True, slots=True)
class SweepResult:
    """Aggregated pending-sweep counts (``deferred`` = still in quiet hours)."""

    delivered: int = 0
    failed: int = 0
    deferred: int = 0


async def deliver_pending(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    *,
    now: datetime,
    policy: RoutingPolicy,
    channels: Mapping[str, NotificationChannel] | None = None,
    sink: EventSink | None = None,
) -> SweepResult:
    """Flush one server's pending notices (the resume path).

    Covers quiet-hours deferrals and pending rows orphaned by a crash
    between insert and delivery (durable attempts, honest states). Failed
    rows are NOT retried here: the 0007 row keeps no failure timestamp, so
    a retry cooldown cannot be computed honestly — escalation or a fresh
    re-evaluation re-arms them instead. Critical severities bypass quiet
    hours; inbox-only pending rows (crash leftovers) are stamped delivered.
    Server-scoped by construction: only this server's rows are touched.
    """
    now = _utc(now)
    registry = channels if channels is not None else {}
    _resolve_channels(policy, registry)
    async with sessions() as session:
        rows = (
            (
                await session.execute(
                    _PENDING_ALERTS,
                    {"server_id": server_id, "cap": MAX_PENDING_PER_SWEEP},
                )
            )
            .mappings()
            .all()
        )
        await session.commit()
    delivered = failed = deferred = 0
    for row in rows:
        severity = _row_severity(row)
        routes = policy.channels_for(severity)
        quiet_blocked = (
            severity != FindingSeverity.CRITICAL.value
            and policy.quiet_hours is not None
            and in_quiet_hours(now, policy.quiet_hours)
        )
        if routes and quiet_blocked:
            deferred += 1
            continue
        if not routes:
            await _stamp(
                sessions,
                row["alert_id"],
                status=DeliveryStatus.DELIVERED,
                delivered_at=now,
            )
            await _emit(
                sink,
                event_type="notification.delivered",
                ts=now,
                row=row,
                server_id=server_id,
                severity=severity,
            )
            delivered += 1
            continue
        ok, _attempted = await _attempt_outbound(
            row, server_id, routes=routes, channels=registry, decided_at=now
        )
        status = DeliveryStatus.DELIVERED if ok else DeliveryStatus.FAILED
        await _stamp(
            sessions,
            row["alert_id"],
            status=status,
            delivered_at=now if ok else None,
        )
        await _emit(
            sink,
            event_type="notification.delivered" if ok else "notification.failed",
            ts=now,
            row=row,
            server_id=server_id,
            severity=severity,
        )
        if ok:
            delivered += 1
        else:
            failed += 1
    return SweepResult(delivered=delivered, failed=failed, deferred=deferred)
