"""bounded observation + notification persistence (card 10-observation-schema)

Schema for contracts.md **C10 rules 3/4** (evidence, notifications) on the
C02 storage pattern:

- ``server_observations`` — one row per collected health sample for a server
  or an adopted application. C10 rule 4: every row reports the frozen
  acquisition ``method`` (``command`` | ``derived`` | ``job_receipt``), the
  sample ``quality`` (``measured`` | ``degraded`` | ``unknown``) and both
  timestamps: ``observed_at`` (sample time reported by the target) and
  ``collected_at`` (controller write time). Staleness is DERIVED, never a
  stored flag: the collector declares the ``fresh_until`` horizon and every
  consumer shares ``mai.contracts.observations.derive_freshness``. An
  ``unknown`` sample carries NO metric/value/observed_at (CHECK) — an
  unavailable target yields unknown, never a zero reading. ``seq`` is
  stamped from the schema's OWN ``server_observations_seq`` Postgres
  sequence: ingestion order is owned by this schema, independent of the
  durable LLM job engine (deterministic collectors keep working when no
  model endpoint is reachable; DBOS system tables stay engine-owned).
- ``observation_findings`` — derived findings (update available, security
  baseline, threshold breach). ``(server_id, kind, dedup_key)`` is UNIQUE:
  repeated observations of the same condition collapse onto one finding
  instead of stacking duplicates. ``kind`` is a bounded collector-owned
  reference (kinds are owned by the collector cards, not frozen here);
  ``severity`` IS the frozen ``info`` | ``warning`` | ``critical``
  vocabulary. ``summary``/``detail`` are bounded human-readable text —
  there is deliberately no payload column that could carry a credential
  dump (C01 rule 7 / brief acceptance 2). ``observation_id`` is ON DELETE
  SET NULL: the retention sweep deletes aged observations without
  orphaning (or deleting) a finding — notifications evidence outlives the
  raw sample.
- ``notification_alerts`` — the C10 rule 3 notification record: per
  server/run/finding with a delivery status and a dedup key.
  ``(server_id, dedup_key)`` is UNIQUE — a duplicate notification key is a
  real integrity violation (repeated polls of the same condition collapse
  onto one notice). ``origin`` discriminates the anchor pair: a
  ``finding`` notice is CREATED with ``finding_id`` and a ``job_result``
  notice with ``run_id`` (frozen in
  ``mai.contracts.observations.validate_alert_shape``); both references
  are ON DELETE SET NULL — the notice is independent of the engine,
  survives source deletion (retention sweep / run GC) with the reference
  severed, and keeps its context in the bounded title/summary/
  context_ref columns. ``delivery_status`` is the frozen ``pending`` |
  ``delivered`` |
  ``failed`` lifecycle with the ``delivered_at`` stamp pinned by CHECK;
  ``acknowledged_at`` records the web-inbox ack — ack does NOT delete the
  source evidence (monitor-api rule). ``context_ref`` is a bounded
  REFERENCE usable in chat, never a payload.

Bounded persistence (brief acceptance 1): every row of all three tables
carries a NOT NULL ``retain_until`` horizon; the retention sweep
(``DELETE ... WHERE retain_until < now()``) is owned by the scheduled
collectors card, the schema encodes the bound per row and indexes it.
Every text column has a ``char_length`` CHECK bound mirrored from
``mai.contracts.observations``.

Chain position: seventh revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0006`` —
this migration serializes strictly after the P08 application catalogue.
The findings/alerts FKs reference ``job_runs`` (0004) and the new tables
only; DBOS system tables stay engine-owned and out of this chain
(``env.py``).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen vocabularies, mirrored from mai.contracts.observations. The focused
# tests extract the live CHECK literals and compare them to the contract
# enums, so a vocabulary drift fails loudly.
_METHODS_SQL = "('command', 'derived', 'job_receipt')"
_QUALITIES_SQL = "('measured', 'degraded', 'unknown')"
_SEVERITIES_SQL = "('info', 'warning', 'critical')"
_ALERT_ORIGINS_SQL = "('finding', 'job_result')"
_DELIVERY_STATUSES_SQL = "('pending', 'delivered', 'failed')"

# Bounded text columns, mirrored from mai.contracts.observations bounds.
_SOURCE_MAX = 64
_METRIC_MAX = 64
_METRIC_VALUE_MAX = 512
_KIND_MAX = 64
_DEDUP_KEY_MAX = 256
_SUMMARY_MAX = 512
_DETAIL_MAX = 2048
_TITLE_MAX = 200
_CONTEXT_REF_MAX = 512


def upgrade() -> None:
    # Observation ingestion order: a first-class Postgres sequence OWNED by
    # this schema (dropped again by downgrade) — deliberately NOT a DBOS /
    # job-table sequence (brief acceptance 2: sequence ownership independent
    # of LLM jobs).
    op.execute(
        sa.text("CREATE SEQUENCE server_observations_seq AS bigint MINVALUE 1")
    )

    op.create_table(
        "server_observations",
        sa.Column(
            "observation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque observation identity (C01 rule 1); minted by "
            "the collector, never a server-side default.",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Observed server (C01 rule 7: server context is "
            "enforced at the storage boundary).",
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Optional adopted-application scope (application "
            "health); NULL = whole-server observation.",
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment="Collector identity (bounded reference, e.g. "
            "'system', 'cron'); kinds of collectors are owned by the "
            "collector cards, the schema bounds the identity only.",
        ),
        sa.Column(
            "method",
            sa.Text(),
            nullable=False,
            comment="C10 rule 4 acquisition method: command | derived | "
            "job_receipt.",
        ),
        sa.Column(
            "quality",
            sa.Text(),
            nullable=False,
            comment="C10 rule 4 sample quality: measured | degraded | "
            "unknown (unavailable target ⇒ unknown, never a zero reading).",
        ),
        sa.Column(
            "metric",
            sa.Text(),
            nullable=True,
            comment="Metric name (e.g. 'disk.used_pct'); NULL when no "
            "sample was taken (quality = unknown).",
        ),
        sa.Column(
            "value",
            sa.Text(),
            nullable=True,
            comment="Bounded textual metric rendering; paired with "
            "metric (both NULL or both present).",
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="C10 rule 4 sample time reported by the target; NULL "
            "when no sample was taken.",
        ),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Controller write time (C01 rule 5: UTC).",
        ),
        sa.Column(
            "fresh_until",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Collector-declared freshness horizon; consumers "
            "derive staleness against their own clock "
            "(derive_freshness) — never a stored flag.",
        ),
        sa.Column(
            "seq",
            sa.BigInteger(),
            server_default=sa.text("nextval('server_observations_seq')"),
            nullable=False,
            comment="Ingestion order from the schema-owned "
            "server_observations_seq — independent of LLM job sequences.",
        ),
        sa.Column(
            "retain_until",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Retention bound (brief acceptance 1): the retention "
            "sweep deletes rows older than their horizon.",
        ),
        sa.CheckConstraint(
            "char_length(source) BETWEEN 1 AND %d" % _SOURCE_MAX,
            name="ck_server_observations_source",
        ),
        sa.CheckConstraint(
            "method IN %s" % _METHODS_SQL,
            name="ck_server_observations_method",
        ),
        sa.CheckConstraint(
            "quality IN %s" % _QUALITIES_SQL,
            name="ck_server_observations_quality",
        ),
        sa.CheckConstraint(
            "metric IS NULL OR char_length(metric) BETWEEN 1 AND %d" % _METRIC_MAX,
            name="ck_server_observations_metric",
        ),
        sa.CheckConstraint(
            "value IS NULL OR char_length(value) BETWEEN 1 AND %d"
            % _METRIC_VALUE_MAX,
            name="ck_server_observations_value",
        ),
        sa.CheckConstraint(
            "(metric IS NULL) = (value IS NULL)",
            name="ck_server_observations_metric_value_pair",
        ),
        sa.CheckConstraint(
            "quality <> 'unknown' OR (metric IS NULL AND value IS NULL "
            "AND observed_at IS NULL)",
            name="ck_server_observations_unknown_has_no_measurement",
        ),
        sa.UniqueConstraint("seq", name="uq_server_observations_seq"),
    )
    op.create_index(
        "ix_server_observations_server",
        "server_observations",
        ["server_id", "collected_at"],
    )
    op.create_index(
        "ix_server_observations_retention",
        "server_observations",
        ["retain_until"],
    )

    op.create_table(
        "observation_findings",
        sa.Column(
            "finding_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque finding identity (C01 rule 1).",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning server (C01 rule 7).",
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Optional adopted-application scope; NULL = "
            "whole-server finding.",
        ),
        sa.Column(
            "observation_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Producing observation; ON DELETE SET NULL — the "
            "retention sweep deletes aged samples without orphaning the "
            "finding.",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["server_observations.observation_id"],
            name="fk_observation_findings_observation",
            ondelete="SET NULL",
        ),
        sa.Column(
            "kind",
            sa.Text(),
            nullable=False,
            comment="Bounded collector-owned finding kind (e.g. "
            "'update.available'); kinds are owned by the collector cards, "
            "not frozen here.",
        ),
        sa.Column(
            "dedup_key",
            sa.Text(),
            nullable=False,
            comment="Deterministic dedup identity within "
            "(server_id, kind); repeated observations collapse onto one "
            "finding.",
        ),
        sa.Column(
            "severity",
            sa.Text(),
            nullable=False,
            comment="Frozen severity: info | warning | critical (drives "
            "notification attention ordering).",
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=False,
            comment="Bounded human-readable summary; no payload column "
            "exists that could carry a credential dump.",
        ),
        sa.Column(
            "detail",
            sa.Text(),
            nullable=True,
            comment="Optional bounded detail (e.g. remediation "
            "suggestion); still a bounded text bound, never a dump.",
        ),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="First observation of the condition (C01 rule 5).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last confirming observation.",
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Condition cleared; NULL while open.",
        ),
        sa.Column(
            "retain_until",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Retention bound (brief acceptance 1).",
        ),
        sa.CheckConstraint(
            "char_length(kind) BETWEEN 1 AND %d" % _KIND_MAX,
            name="ck_observation_findings_kind",
        ),
        sa.CheckConstraint(
            "char_length(dedup_key) BETWEEN 1 AND %d" % _DEDUP_KEY_MAX,
            name="ck_observation_findings_dedup_key",
        ),
        sa.CheckConstraint(
            "severity IN %s" % _SEVERITIES_SQL,
            name="ck_observation_findings_severity",
        ),
        sa.CheckConstraint(
            "char_length(summary) BETWEEN 1 AND %d" % _SUMMARY_MAX,
            name="ck_observation_findings_summary",
        ),
        sa.CheckConstraint(
            "detail IS NULL OR char_length(detail) BETWEEN 1 AND %d" % _DETAIL_MAX,
            name="ck_observation_findings_detail",
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= opened_at",
            name="ck_observation_findings_resolved_after_opened",
        ),
        sa.UniqueConstraint(
            "server_id",
            "kind",
            "dedup_key",
            name="uq_observation_findings_dedup",
        ),
    )
    op.create_index(
        "ix_observation_findings_server",
        "observation_findings",
        ["server_id"],
    )
    op.create_index(
        "ix_observation_findings_retention",
        "observation_findings",
        ["retain_until"],
    )

    op.create_table(
        "notification_alerts",
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque notice identity (C01 rule 1).",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning server (C01 rule 7); the dedup key is scoped "
            "per server, so the same condition on two servers yields two "
            "notices.",
        ),
        sa.Column(
            "origin",
            sa.Text(),
            nullable=False,
            comment="C10 rule 3 anchor discriminator: finding | "
            "job_result. The CREATION shape (finding notices carry "
            "finding_id, job-result notices carry run_id) is frozen in "
            "mai.contracts.observations.validate_alert_shape, not a "
            "CHECK: both references are ON DELETE SET NULL — the notice "
            "is independent of the engine and survives source deletion "
            "(retention sweep / run GC) with the reference severed, and "
            "a permanent NOT NULL anchor would make that legal severance "
            "impossible.",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Durable job run reference (0004 job_runs) for "
            "job-result notices; ON DELETE SET NULL — the notice is "
            "independent of the engine and survives run deletion.",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["job_runs.run_id"], name="fk_notification_alerts_run",
            ondelete="SET NULL",
        ),
        sa.Column(
            "finding_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Source finding reference; ON DELETE SET NULL — the "
            "retention sweep deletes aged findings without deleting the "
            "notice (ack/audit evidence outlives the finding).",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["observation_findings.finding_id"],
            name="fk_notification_alerts_finding",
            ondelete="SET NULL",
        ),
        sa.Column(
            "dedup_key",
            sa.Text(),
            nullable=False,
            comment="C10 rule 3 dedup key, scoped per server_id "
            "(UNIQUE): repeated polls of the same condition collapse "
            "onto one notice; a duplicate key is a real violation.",
        ),
        sa.Column(
            "title",
            sa.Text(),
            nullable=False,
            comment="Bounded inbox title.",
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
            comment="Optional bounded inbox body.",
        ),
        sa.Column(
            "context_ref",
            sa.Text(),
            nullable=True,
            comment="Bounded REFERENCE usable in chat (monitor-api); "
            "never a payload.",
        ),
        sa.Column(
            "delivery_status",
            sa.Text(),
            nullable=False,
            comment="C10 rule 3 delivery lifecycle: pending | delivered "
            "| failed (web inbox first; no messenger channel "
            "vocabulary yet).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Notice creation time (C01 rule 5).",
        ),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Delivery stamp; pinned by CHECK to the "
            "delivered status (equivalence).",
        ),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Web-inbox ack time; ack does NOT delete the source "
            "evidence (monitor-api rule).",
        ),
        sa.Column(
            "retain_until",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Retention bound (brief acceptance 1).",
        ),
        sa.CheckConstraint(
            "char_length(dedup_key) BETWEEN 1 AND %d" % _DEDUP_KEY_MAX,
            name="ck_notification_alerts_dedup_key",
        ),
        sa.CheckConstraint(
            "origin IN %s" % _ALERT_ORIGINS_SQL,
            name="ck_notification_alerts_origin",
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND %d" % _TITLE_MAX,
            name="ck_notification_alerts_title",
        ),
        sa.CheckConstraint(
            "summary IS NULL OR char_length(summary) BETWEEN 1 AND %d"
            % _SUMMARY_MAX,
            name="ck_notification_alerts_summary",
        ),
        sa.CheckConstraint(
            "context_ref IS NULL OR char_length(context_ref) BETWEEN 1 AND %d"
            % _CONTEXT_REF_MAX,
            name="ck_notification_alerts_context_ref",
        ),
        sa.CheckConstraint(
            "delivery_status IN %s" % _DELIVERY_STATUSES_SQL,
            name="ck_notification_alerts_delivery_status",
        ),
        sa.CheckConstraint(
            "(delivery_status = 'delivered') = (delivered_at IS NOT NULL)",
            name="ck_notification_alerts_delivery_stamp",
        ),
        sa.CheckConstraint(
            "acknowledged_at IS NULL OR acknowledged_at >= created_at",
            name="ck_notification_alerts_ack_after_created",
        ),
        sa.UniqueConstraint(
            "server_id",
            "dedup_key",
            name="uq_notification_alerts_dedup",
        ),
    )
    op.create_index(
        "ix_notification_alerts_server",
        "notification_alerts",
        ["server_id", "created_at"],
    )
    op.create_index(
        "ix_notification_alerts_retention",
        "notification_alerts",
        ["retain_until"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_alerts_retention", table_name="notification_alerts"
    )
    op.drop_index("ix_notification_alerts_server", table_name="notification_alerts")
    op.drop_table("notification_alerts")
    op.drop_index(
        "ix_observation_findings_retention", table_name="observation_findings"
    )
    op.drop_index("ix_observation_findings_server", table_name="observation_findings")
    op.drop_table("observation_findings")
    op.drop_index(
        "ix_server_observations_retention", table_name="server_observations"
    )
    op.drop_index("ix_server_observations_server", table_name="server_observations")
    op.drop_table("server_observations")
    op.execute(sa.text("DROP SEQUENCE server_observations_seq"))
