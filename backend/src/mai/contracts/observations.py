"""Frozen observation vocabulary: health observations, findings, alerts.

Frozen schema producer for contracts.md **C10** (evidence, notifications,
testing and future seams), against:

- **C10 rule 3** — notifications persist per server/run/finding with a
  delivery status and a dedup key. ``AlertOrigin`` pins the two anchors a
  notice can have (a domain ``finding`` or a durable ``job_result``);
  ``DeliveryStatus`` pins the delivery lifecycle of the web inbox (messenger
  adapters are a non-goal now, so there is no channel vocabulary yet).
  ``(server_id, dedup_key)`` is UNIQUE in migration ``0007_observations`` —
  repeated polls of the same condition collapse onto one notice; a duplicate
  notification key is a real integrity violation, not a caller invariant.
- **C10 rule 4** — metrics collection reports timestamp/method/quality.
  ``ObservationMethod`` is the frozen acquisition vocabulary (bounded target
  ``command``, controller-side ``derived``, durable-engine ``job_receipt``);
  ``ObservationQuality`` is the frozen health of the sample itself
  (``measured`` | ``degraded`` | ``unknown``) — an unavailable target yields
  ``unknown`` with NO metric/value/observed_at, never a zero reading (the
  10-system rule "stale/unknown, not zero usage"). Staleness is derived, not
  stored as a mutable flag: each observation carries ``observed_at`` (sample
  time) and ``fresh_until`` (collector-declared freshness horizon);
  :func:`derive_freshness` is the frozen derivation every consumer shares
  (monitor API "queries with freshness", dashboard truthful states).
- **Bounded persistence (brief acceptance 1)** — every row carries a NOT NULL
  ``retain_until`` horizon; the retention sweep
  (``DELETE ... WHERE retain_until < now()``) is owned by the scheduled
  collectors card, the schema encodes the bound per row. Every text column
  has a ``char_length`` CHECK (the bounds constants below), so the schema
  structurally cannot store unbounded dumps; there are deliberately NO JSONB
  payload columns and NO credential-shaped columns (brief acceptance 2: raw
  credential dumps have no storage home — only bounded, human-readable
  references and summaries).
- **Sequence ownership (brief acceptance 2)** — observation ingestion order
  is stamped from the schema's own ``server_observations_seq`` Postgres
  sequence (migration ``0007_observations``), independent of the durable
  LLM job engine: deterministic collectors keep inserting when no model
  endpoint is reachable and never contend with job/step/event sequences
  (DBOS system tables are engine-owned and out of this chain).
- **Finding dedup** — ``observation_findings`` UNIQUE
  ``(server_id, kind, dedup_key)``: repeated observations of the same
  condition update one finding instead of stacking duplicates. ``kind`` is a
  bounded collector-owned reference (``update.available``,
  ``security.baseline``, …), NOT a frozen vocabulary: collector cards own
  their kinds, the schema only bounds the identity. ``FindingSeverity`` IS
  frozen (``info`` | ``warning`` | ``critical``) — it drives notification
  attention ordering.
- **C01 rule 1 / rule 5 / rule 7** — identities are opaque lowercase
  hyphenated UUIDs minted by the application (never server-generated
  defaults); timestamps are timezone-aware UTC ``timestamptz``; every row
  carries its owning ``server_id`` so scope filtering stays a storage
  boundary.

This module is pure: it reads no process environment, filesystem or network
state, so validation is SSR/worker/replay safe and evidence runs cannot leak
ambient secret material.

Consumers: deterministic collectors (10-system, 10-network, 10-process,
10-cron, 10-security, 10-update-check), the notification service
(10-notifications), the monitor API (10-monitor-api) and the web dashboard
(10-web-dashboard). The SQL home is migration ``0007_observations``; the
focused tests assert migration CHECK vocabularies and bounds stay identical
to this module, so a vocabulary drift fails loudly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum


class ObservationMethod(StrEnum):
    """How the sample was acquired (C10 rule 4 ``method``)."""

    COMMAND = "command"
    DERIVED = "derived"
    JOB_RECEIPT = "job_receipt"


class ObservationQuality(StrEnum):
    """Health of the sample itself (C10 rule 4 ``quality``)."""

    MEASURED = "measured"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class ObservationFreshness(StrEnum):
    """Derived staleness class consumers display (never a stored flag)."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class FindingSeverity(StrEnum):
    """Frozen finding severity (drives notification attention)."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertOrigin(StrEnum):
    """Frozen notification anchor (C10 rule 3: per finding or job result)."""

    FINDING = "finding"
    JOB_RESULT = "job_result"


class DeliveryStatus(StrEnum):
    """Frozen delivery lifecycle of the web inbox (C10 rule 3)."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


# Bounded text columns (migration ``0007_observations`` mirrors these with
# ``char_length`` CHECK constraints; the tests extract the live bounds and
# compare them to these constants, so drift fails loudly). Bounds are
# identities/references/summaries — deliberately small; there is no column
# that could carry a credential dump or a command transcript.
SOURCE_MAX_CHARS = 64
METRIC_MAX_CHARS = 64
METRIC_VALUE_MAX_CHARS = 512
KIND_MAX_CHARS = 64
DEDUP_KEY_MAX_CHARS = 256
SUMMARY_MAX_CHARS = 512
DETAIL_MAX_CHARS = 2048
TITLE_MAX_CHARS = 200
CONTEXT_REF_MAX_CHARS = 512


def validate_alert_shape(
    origin: AlertOrigin | str,
    run_id: uuid.UUID | None,
    finding_id: uuid.UUID | None,
) -> None:
    """Frozen CREATION shape of a notification alert (C10 rule 3).

    A ``finding`` notice is created with its source ``finding_id``; a
    ``job_result`` notice with its originating ``run_id``. This is a
    creation-time contract, deliberately NOT a permanent CHECK: both
    references are ON DELETE SET NULL in migration ``0007_observations``
    (the retention sweep / run GC legally sever them, and the notice —
    with its ack state and bounded context — must survive), so a stored
    row may legitimately have a NULL reference later. Producers call this
    validator before insert; the storage boundary enforces the reference
    TARGET (real FK) and the dedup key.
    """
    if origin == AlertOrigin.FINDING and finding_id is None:
        raise ValueError("a finding notice requires finding_id")
    if origin == AlertOrigin.JOB_RESULT and run_id is None:
        raise ValueError("a job_result notice requires run_id")


def derive_freshness(
    quality: ObservationQuality | str,
    observed_at: datetime | None,
    fresh_until: datetime | None,
    now: datetime,
) -> ObservationFreshness:
    """Frozen staleness derivation shared by every consumer.

    ``unknown`` when no sample was actually taken (``quality='unknown'`` or
    the sample time/horizon is absent — the collector could not observe);
    ``fresh`` while ``now`` is within the collector-declared horizon;
    ``stale`` once the horizon has passed. A stored ``is_stale`` flag would
    go stale itself; the derivation is computed against the consumer's clock.
    Naive ``now`` is rejected: timestamps are timezone-aware UTC (C01 rule 5).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if (
        quality == ObservationQuality.UNKNOWN
        or observed_at is None
        or fresh_until is None
    ):
        return ObservationFreshness.UNKNOWN
    if fresh_until.tzinfo is None or observed_at.tzinfo is None:
        raise ValueError("observation timestamps must be timezone-aware")
    if fresh_until >= now:
        return ObservationFreshness.FRESH
    return ObservationFreshness.STALE
