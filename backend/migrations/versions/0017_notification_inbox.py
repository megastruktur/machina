"""unified notification inbox + machine ingest keys (notification-inbox, task nbi-schema)

Extends the 0007 ``notification_alerts`` table into the unified inbox and
adds the ``ingest_keys`` machine-principal table, per the frozen
notification-inbox schema delta (nbi_PLAN.md «Schema delta», from
nbi_RESEARCH.md Rev 2 §R2-F1/§R2-F2):

- ``notification_alerts.server_id`` drops NOT NULL: NULL is a GLOBAL notice
  (no sentinel UUID). The existing composite ``UNIQUE(server_id,
  dedup_key)`` (0007) is untouched — it cannot arbitrate NULL-server rows
  (NULL-distinct semantics), so global rows dedup on the new partial unique
  index ``uq_notification_alerts_dedup_global ... WHERE server_id IS NULL``.
- ``origin`` CHECK widens to ``('finding', 'job_result', 'external')`` —
  the constraint keeps its 0007 name, so the focused vocabulary tests keep
  extracting and comparing the live literals.
- ``severity`` becomes a real column (TEXT NOT NULL DEFAULT 'info', CHECK
  ``info`` | ``warning`` | ``critical``), replacing the ``[severity=<v>]``
  summary-tag grammar for the reader. The upgrade backfills it with the
  FROZEN reader precedence (``mai.api.monitoring._alert_severity``,
  monitoring.py:349-362, R2-F4 resolution): live anchor join FIRST (the
  collector's freshest ``observation_findings.severity`` through
  ``finding_id``), the service writer's exact ``[severity=<v>]`` summary
  tag SECOND (case-sensitive, anchored at the end — the reader regex),
  ``'info'`` fallback THIRD. Existing rows keep exactly the meaning the
  reader derives today; the focused tests prove the backfill row-for-row
  against the frozen reader on a populated fixture.
- ``source`` (NULL, 1..64 chars): sender identity of external notices
  (defaults to the ingest key's label); NULL for finding/job_result.
- ``body`` (NULL, 1..4096 chars): full external message text; NULL for
  finding/job_result (the summary remains their text).
- List index ``(created_at DESC, alert_id DESC)`` (the canonical inbox
  ordering) and partial unread index ``(server_id, created_at) WHERE
  acknowledged_at IS NULL``.
- ``ingest_keys``: the machine ingest principal (auth ADR §13).
  ``key_hash`` is the SHA-256 hex of the bearer key (exactly 64 lowercase
  hex chars, UNIQUE — the plaintext is returned once at mint time and
  never stored); ``label`` is bounded 1..64 non-blank; ``server_id`` NULL
  = global key, deliberately carrying NO foreign key (server profiles are
  files, not rows — the 0007 convention — so unknown/deleted servers must
  not break key validation; scope is enforced at the API boundary);
  ``rate_window_start``/``rate_window_count`` is the durable fixed window
  (60/min, restart-safe).

Bounds mirror ``mai.contracts.observations`` (``MAX_NOTIFICATION_SOURCE_CHARS``,
``MAX_NOTIFICATION_BODY_CHARS``); the focused tests extract the live CHECK
definitions and compare them to the contract constants, so drift fails
loudly (0007 discipline).

``downgrade('0016')`` reverses honestly and is DESTRUCTIVE FOR NEW DATA —
only data created by this revision cannot survive the restored 0007 shape:
it deletes global/external rows (NULL ``server_id`` or ``origin=
'external'``) and the ``ingest_keys`` table, drops the new columns and
indexes, restores the narrowed ``origin`` CHECK and the ``server_id`` NOT
NULL. Server-scoped finding/job_result notices survive; their backfilled
``severity`` is discarded with the column (the 0016-era reader re-derives
it from the anchor/tag).

Chain position: seventeenth revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0016``.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-13

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen vocabularies, mirrored from mai.contracts.observations (0007
# discipline: the focused tests extract the live CHECK literals and compare
# them to the contract enums, so a vocabulary drift fails loudly). The
# narrow origins tuple is the EXACT 0007 literal, restored verbatim by the
# downgrade.
_SEVERITIES_SQL = "('info', 'warning', 'critical')"
_ALERT_ORIGINS_WIDE_SQL = "('finding', 'job_result', 'external')"
_ALERT_ORIGINS_NARROW_SQL = "('finding', 'job_result')"

# Bounded text columns, mirrored from mai.contracts.observations.
_SOURCE_MAX = 64
_BODY_MAX = 4096
_LABEL_MAX = 64
_KEY_HASH_LENGTH = 64

# The exact frozen reader precedence (R2-F4) as one backfill expression —
# ``_alert_severity(anchor_severity, summary)`` rendered in SQL:
# 1. live anchor join FIRST: ``observation_findings.severity`` through
#    ``finding_id`` (the anchor column is CHECK-pinned to the severity
#    vocabulary, so every non-NULL anchor is a valid reader value; a
#    severed/absent anchor yields NULL, exactly like the LEFT JOIN);
# 2. the service writer's exact ``[severity=<v>]`` summary-tag grammar
#    SECOND (case-sensitive, anchored at the end of the summary — the
#    ``(\n)?`` mirrors Python's trailing-newline ``$`` tolerance);
# 3. ``'info'`` fallback THIRD.
_SEVERITY_BACKFILL_SQL = r"""
UPDATE notification_alerts a
SET severity = COALESCE(
        (SELECT f.severity FROM observation_findings f
          WHERE f.finding_id = a.finding_id),
        (regexp_match(a.summary,
                      '\[severity=(info|warning|critical)\](\n)?$'))[1],
        'info')
"""


def upgrade() -> None:
    """Widen the inbox schema; backfill severity with the frozen reader."""
    # 1. Real severity column. The NOT NULL + server default populate every
    #    existing row with 'info'; the backfill below then applies the
    #    frozen reader precedence before the vocabulary CHECK validates.
    op.add_column(
        "notification_alerts",
        sa.Column(
            "severity",
            sa.Text(),
            server_default=sa.text("'info'"),
            nullable=False,
            comment="Real severity: info | warning | critical (drives "
            "attention ordering). Backfilled by THIS migration with the "
            "frozen reader precedence — live anchor join first, "
            "[severity=<v>] summary tag second, 'info' fallback (R2-F4) — "
            "replacing the tag grammar for the reader.",
        ),
    )
    op.execute(sa.text(_SEVERITY_BACKFILL_SQL))
    op.create_check_constraint(
        "ck_notification_alerts_severity",
        "notification_alerts",
        "severity IN %s" % _SEVERITIES_SQL,
    )

    # 2. External-notice columns (NULL for finding/job_result rows).
    op.add_column(
        "notification_alerts",
        sa.Column(
            "source",
            sa.Text(),
            nullable=True,
            comment="Sender identity of external notices (defaults to the "
            "ingest key's label); NULL for finding/job_result.",
        ),
    )
    op.create_check_constraint(
        "ck_notification_alerts_source",
        "notification_alerts",
        "source IS NULL OR char_length(source) BETWEEN 1 AND %d"
        % _SOURCE_MAX,
    )
    op.add_column(
        "notification_alerts",
        sa.Column(
            "body",
            sa.Text(),
            nullable=True,
            comment="Full external message text; NULL for "
            "finding/job_result (the summary remains their text).",
        ),
    )
    op.create_check_constraint(
        "ck_notification_alerts_body",
        "notification_alerts",
        "body IS NULL OR char_length(body) BETWEEN 1 AND %d" % _BODY_MAX,
    )

    # 3. Widen the origin vocabulary (same constraint name as 0007, so the
    #    focused vocabulary tests keep comparing the live literals).
    op.drop_constraint(
        "ck_notification_alerts_origin",
        "notification_alerts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_notification_alerts_origin",
        "notification_alerts",
        "origin IN %s" % _ALERT_ORIGINS_WIDE_SQL,
    )

    # 4. Global notices: NULL server scope (no sentinel UUID). The
    #    server-scoped uniqueness (0007) is untouched; NULL rows never
    #    collide under it (NULL-distinct semantics).
    op.alter_column(
        "notification_alerts",
        "server_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
        comment="Owning server (C01 rule 7); the dedup key is scoped per "
        "server, so the same condition on two servers yields two notices. "
        "NULL = a GLOBAL notice (external push); global rows dedup on the "
        "partial unique index uq_notification_alerts_dedup_global.",
    )

    # 5. Inbox indexes: canonical list ordering + partial unread index +
    #    the global dedup arbiter (partial unique — NULL-distinct rows
    #    need the WHERE clause to dedup at all).
    op.create_index(
        "ix_notification_alerts_created_at",
        "notification_alerts",
        [sa.text("created_at DESC"), sa.text("alert_id DESC")],
    )
    op.create_index(
        "ix_notification_alerts_unread",
        "notification_alerts",
        ["server_id", "created_at"],
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )
    op.create_index(
        "uq_notification_alerts_dedup_global",
        "notification_alerts",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("server_id IS NULL"),
    )

    # 6. Machine ingest principal (auth ADR §13). server_id is a scope pin
    #    WITHOUT a foreign key: server profiles are files, not rows, so an
    #    unknown/deleted server must not break key validation (scope is
    #    enforced at the API boundary, fail-closed there).
    op.create_table(
        "ingest_keys",
        sa.Column(
            "key_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque key identity (C01 rule 1).",
        ),
        sa.Column(
            "key_hash",
            sa.CHAR(_KEY_HASH_LENGTH),
            nullable=False,
            comment="SHA-256 hex of the bearer key: exactly 64 lowercase "
            "hex chars (CHECK), UNIQUE. The plaintext is returned ONCE at "
            "mint time and never stored.",
        ),
        sa.Column(
            "label",
            sa.Text(),
            nullable=False,
            comment="Human-readable key label (1..64 non-blank); also the "
            "default ``source`` of notices published with the key.",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Scope pin; NULL = global key (may only append "
            "NULL-server notices). Deliberately NO foreign key: server "
            "profiles are files, not rows (0007 convention); scope is "
            "enforced at the API boundary.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Key mint time (C01 rule 5).",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Optional expiry; expired keys fail closed on ingest.",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Revocation stamp; revoked keys fail closed on the "
            "next ingest.",
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last accepted ingest stamp.",
        ),
        sa.Column(
            "rate_window_start",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Durable fixed-rate-window start (INGEST_RATE_WINDOW "
            "= 60 requests/minute, restart-safe); NULL = no open window.",
        ),
        sa.Column(
            "rate_window_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="Requests counted in the open window (>= 0).",
        ),
        sa.CheckConstraint(
            "char_length(btrim(label)) BETWEEN 1 AND %d" % _LABEL_MAX,
            name="ck_ingest_keys_label",
        ),
        sa.CheckConstraint(
            "key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_ingest_keys_key_hash",
        ),
        sa.CheckConstraint(
            "rate_window_count >= 0",
            name="ck_ingest_keys_rate_window_count",
        ),
        sa.UniqueConstraint("key_hash", name="uq_ingest_keys_key_hash"),
    )


def downgrade() -> None:
    """Restore the 0007 shape; DESTRUCTIVE FOR 0017-ERA DATA (documented).

    Global/external rows and ingest keys cannot survive the restored NOT
    NULL / narrowed CHECK; server-scoped finding/job_result notices (and
    their findings) survive untouched.
    """
    op.execute(
        sa.text(
            "DELETE FROM notification_alerts "
            "WHERE server_id IS NULL OR origin = 'external'"
        )
    )
    op.drop_table("ingest_keys")
    op.drop_index(
        "ix_notification_alerts_unread", table_name="notification_alerts"
    )
    op.drop_index(
        "ix_notification_alerts_created_at", table_name="notification_alerts"
    )
    op.drop_index(
        "uq_notification_alerts_dedup_global",
        table_name="notification_alerts",
    )
    op.drop_column("notification_alerts", "body")
    op.drop_column("notification_alerts", "source")
    op.drop_column("notification_alerts", "severity")
    op.drop_constraint(
        "ck_notification_alerts_origin",
        "notification_alerts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_notification_alerts_origin",
        "notification_alerts",
        "origin IN %s" % _ALERT_ORIGINS_NARROW_SQL,
    )
    op.alter_column(
        "notification_alerts",
        "server_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
        comment="Owning server (C01 rule 7); the dedup key is scoped per "
        "server, so the same condition on two servers yields two notices.",
    )
