"""application catalogue projection + reconciliation journal (card 08-adoption)

Schema for contracts.md **C09 rules 1/6 on the C02 storage pattern**:

- ``application_projection`` — the SQL projection of the canonical adoption
  manifest (``servers/<server_id>/applications/<application_id>/manifest.yaml``).
  One row per adopted application: the C02 rule-4 version triple (integer
  content ``revision`` starting at 1, ``schema_revision``, SHA-256
  ``sha256``) plus the projected manifest fields (display name, frozen
  adoptable origin vocabulary, the reference selectors discovery matches on,
  and the external deployment paths as REFERENCED metadata — C09 rule 6:
  paths only, the manifest text never enters SQL). The reference CHECK
  (at least one selector) mirrors the adoption request rule; the origin
  CHECK pins the two origins discovery can tag ``managed`` (standalone
  containers have no frozen selector and are refused at adoption).
  ``adopted_at`` keeps the first reconcile time; ``reconciled_at`` advances
  with every confirmed projection (W2 consumers read after reconcile or
  show stale).
- ``application_projection_journal`` — one row per write intent with the
  frozen C02 rule-5 phases ``prepared -> file_committed -> reconciled``
  (structural mirror of the ``profile_projection_journal`` shape). Upsert
  intents carry the manifest payload only while ``prepared`` (W1
  re-execution); the ``file_committed`` advance NULLs it and CHECK
  constraints forbid a payload in every later phase. Revoke intents are the
  W4 tombstone: journaled BEFORE the canonical manifest is removed, purged
  from the projection only after the reconcile confirms.

A partial unique index enforces at most one live (non-reconciled) intent per
``(server_id, application_id)`` — a server owns many applications, so the
per-server single-writer mirror of the profile journal is scoped to the
application identity (C05 rule 6 serialization stays a caller invariant).

Chain position: sixth revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0005`` —
this migration serializes strictly after the P07 knowledge schema. DBOS
system tables stay engine-owned and out of this chain (``env.py``).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

_RECONCILED = "reconciled"

# Frozen vocabularies, mirrored from mai.applications (discover +
# adopt). A standalone container cannot become a managed record (no frozen
# selector), so its origin is absent here by design, not by omission.
_ORIGINS_SQL = "('compose', 'systemd_unit')"


def upgrade() -> None:
    op.create_table(
        "application_projection",
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque application identity (C01 rule 1); canonical "
            "key and discovery managed-tagging reference.",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning server (C01 rule 7: server context is enforced "
            "at the storage boundary).",
        ),
        sa.Column(
            "name",
            sa.Text(),
            nullable=False,
            comment="Admin-controlled display label; never an identity.",
        ),
        sa.Column(
            "origin",
            sa.Text(),
            nullable=False,
            comment="Adoptable discovery origin vocabulary: compose | "
            "systemd_unit (standalone containers are refused at adoption).",
        ),
        sa.Column(
            "compose_project",
            sa.Text(),
            nullable=True,
            comment="Reference selector: exact compose project name "
            "(discovery managed matching).",
        ),
        sa.Column(
            "working_dir",
            sa.Text(),
            nullable=True,
            comment="Reference selector: compose working dir as reported "
            "(trailing-slash-insensitive matching).",
        ),
        sa.Column(
            "unit_name",
            sa.Text(),
            nullable=True,
            comment="Reference selector: exact systemd unit name.",
        ),
        sa.Column(
            "external_working_dir",
            sa.Text(),
            nullable=True,
            comment="External deployment path, REFERENCED metadata only "
            "(C09 rule 6: original target paths, never moved/copied; the "
            "controller never opens these paths).",
        ),
        sa.Column(
            "external_config_files",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
            comment="External compose config file paths (references only).",
        ),
        sa.Column(
            "external_environment_files",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
            comment="External environment-file PATHS (references only; "
            "contents were never collected by discovery).",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            comment="Canonical content revision (manifest store triple).",
        ),
        sa.Column(
            "schema_revision",
            sa.Integer(),
            nullable=False,
            comment="Frozen manifest schema version (MANIFEST_SCHEMA_VERSION).",
        ),
        sa.Column(
            "sha256",
            sa.String(length=64),
            nullable=False,
            comment="SHA-256 of the canonical manifest bytes.",
        ),
        sa.Column(
            "adopted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC time of the first confirmed projection (C01 rule 5).",
        ),
        sa.Column(
            "reconciled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC time of the last confirmed reconcile.",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_application_projection_revision"),
        sa.CheckConstraint(
            "char_length(sha256) = 64", name="ck_application_projection_sha256"
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 200",
            name="ck_application_projection_name",
        ),
        sa.CheckConstraint(
            "origin IN %s" % _ORIGINS_SQL,
            name="ck_application_projection_origin",
        ),
        sa.CheckConstraint(
            "compose_project IS NOT NULL OR working_dir IS NOT NULL "
            "OR unit_name IS NOT NULL",
            name="ck_application_projection_reference",
        ),
        sa.CheckConstraint(
            "compose_project IS NULL OR char_length(compose_project) BETWEEN 1 AND 512",
            name="ck_application_projection_compose_project",
        ),
        sa.CheckConstraint(
            "working_dir IS NULL OR char_length(working_dir) BETWEEN 1 AND 512",
            name="ck_application_projection_working_dir",
        ),
        sa.CheckConstraint(
            "unit_name IS NULL OR char_length(unit_name) BETWEEN 1 AND 512",
            name="ck_application_projection_unit_name",
        ),
        sa.CheckConstraint(
            "external_working_dir IS NULL "
            "OR char_length(external_working_dir) BETWEEN 1 AND 1024",
            name="ck_application_projection_external_working_dir",
        ),
        sa.CheckConstraint(
            "cardinality(external_config_files) <= 64",
            name="ck_application_projection_config_files",
        ),
        sa.CheckConstraint(
            "cardinality(external_environment_files) <= 64",
            name="ck_application_projection_environment_files",
        ),
    )
    # Storage-boundary scope filter key (C01 rule 7): per-server listing and
    # the selector-overlap check resolve through this index.
    op.create_index(
        "ix_application_projection_server",
        "application_projection",
        ["server_id"],
    )

    op.create_table(
        "application_projection_journal",
        sa.Column(
            "intent_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque intent identity (C01 rule 1); retry key.",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Target server; the live-intent index is scoped per "
            "(server_id, application_id).",
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Target application; at most one live intent per value.",
        ),
        sa.Column(
            "op",
            sa.String(length=16),
            nullable=False,
            comment="Frozen journal operation: upsert | delete (tombstone).",
        ),
        sa.Column(
            "phase",
            sa.String(length=16),
            nullable=False,
            comment="Frozen phase: prepared | file_committed | reconciled.",
        ),
        sa.Column(
            "expected_revision",
            sa.Integer(),
            nullable=False,
            comment="Revision the caller observed (0 = create/absent).",
        ),
        sa.Column(
            "base_sha256",
            sa.String(length=64),
            nullable=True,
            comment="Hash observed at expected_revision (W1 comparison).",
        ),
        sa.Column(
            "target_revision",
            sa.Integer(),
            nullable=True,
            comment="Upsert only: intended post-commit revision.",
        ),
        sa.Column(
            "target_sha256",
            sa.String(length=64),
            nullable=True,
            comment="Upsert only: intended content hash.",
        ),
        sa.Column(
            "schema_revision",
            sa.Integer(),
            nullable=True,
            comment="Upsert only: frozen schema version at commit time.",
        ),
        sa.Column(
            "payload",
            sa.Text(),
            nullable=True,
            comment="Upsert only, prepared phase only: canonical manifest "
            "for W1 re-execution; NULLed at file_committed.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "op IN ('upsert', 'delete')", name="ck_application_projection_journal_op"
        ),
        sa.CheckConstraint(
            "phase IN ('prepared', 'file_committed', 'reconciled')",
            name="ck_application_projection_journal_phase",
        ),
        sa.CheckConstraint(
            "expected_revision >= 0",
            name="ck_application_projection_journal_expected_revision",
        ),
        sa.CheckConstraint(
            "target_revision IS NULL OR target_revision >= 1",
            name="ck_application_projection_journal_target_revision",
        ),
        sa.CheckConstraint(
            "op <> 'upsert' OR (target_revision IS NOT NULL "
            "AND target_sha256 IS NOT NULL AND schema_revision IS NOT NULL)",
            name="ck_application_projection_journal_upsert_targets",
        ),
        sa.CheckConstraint(
            "op <> 'delete' OR (target_revision IS NULL "
            "AND target_sha256 IS NULL AND schema_revision IS NULL "
            "AND payload IS NULL)",
            name="ck_application_projection_journal_delete_targets",
        ),
        sa.CheckConstraint(
            "phase <> 'prepared' OR op <> 'upsert' OR payload IS NOT NULL",
            name="ck_application_projection_journal_upsert_payload_prepared",
        ),
        sa.CheckConstraint(
            "phase = 'prepared' OR payload IS NULL",
            name="ck_application_projection_journal_payload_only_prepared",
        ),
    )
    # One live intent per (server_id, application_id) — the storage backstop
    # behind the per-server single-writer invariant (C05 rule 6); reconciled
    # history is exempt so retries stay idempotent and tombstones can be
    # purged.
    op.create_index(
        "uq_application_projection_journal_live",
        "application_projection_journal",
        ["server_id", "application_id"],
        unique=True,
        postgresql_where=sa.text(f"phase <> '{_RECONCILED}'"),
    )
    op.create_index(
        "ix_application_projection_journal_server",
        "application_projection_journal",
        ["server_id"],
    )
    op.create_index(
        "ix_application_projection_journal_phase",
        "application_projection_journal",
        ["phase"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_application_projection_journal_phase",
        table_name="application_projection_journal",
    )
    op.drop_index(
        "ix_application_projection_journal_server",
        table_name="application_projection_journal",
    )
    op.drop_index(
        "uq_application_projection_journal_live",
        table_name="application_projection_journal",
    )
    op.drop_table("application_projection_journal")
    op.drop_index(
        "ix_application_projection_server",
        table_name="application_projection",
    )
    op.drop_table("application_projection")
