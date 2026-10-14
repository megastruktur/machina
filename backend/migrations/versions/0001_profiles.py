"""profiles projection + reconciliation journal (card 02-profile-projection)

Schema for contracts.md **C02 rules 4–5**:

- ``profile_projection`` — the SQL projection of the canonical version triple
  (integer content ``revision`` starting at 1, ``schema_revision``,
  SHA-256 ``sha256``). It stores identity/version metadata only; the
  canonical document text never enters SQL (no double-written docs).
- ``profile_projection_journal`` — one row per write intent with the frozen
  phases ``prepared -> file_committed -> reconciled``. Upsert intents carry
  the document payload only while ``prepared`` (W1 re-execution); the
  ``file_committed`` advance NULLs it and CHECK constraints forbid a payload
  in every later phase. Delete intents are the W4 tombstone: journaled
  before the canonical file is removed, purged only after the projection
  confirms.

A partial unique index enforces at most one live (non-reconciled) intent per
``server_id`` — the storage-level mirror of the per-server single-writer
invariant (C05 rule 6).

Chain position: first revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``None``.
DBOS system tables stay engine-owned and out of this chain.

Revision ID: 0001
Revises:
Create Date: 2026-10-13

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_RECONCILED = "reconciled"


def upgrade() -> None:
    op.create_table(
        "profile_projection",
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque server identity (C01 rule 1); canonical key.",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            comment="Canonical content revision (file store triple).",
        ),
        sa.Column(
            "schema_revision",
            sa.Integer(),
            nullable=False,
            comment="Frozen producer schema version (SCHEMA_VERSION).",
        ),
        sa.Column(
            "sha256",
            sa.String(length=64),
            nullable=False,
            comment="SHA-256 of the canonical document bytes.",
        ),
        sa.Column(
            "first_reconciled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC timestamp of the first projection (C01 rule 5).",
        ),
        sa.Column(
            "reconciled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC timestamp of the last confirmed reconcile.",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_profile_projection_revision"),
        sa.CheckConstraint(
            "char_length(sha256) = 64", name="ck_profile_projection_sha256"
        ),
    )
    op.create_table(
        "profile_projection_journal",
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
            comment="Target server; at most one live intent per value.",
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
            comment="Upsert only, prepared phase only: canonical document "
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
            "op IN ('upsert', 'delete')", name="ck_ppj_op"
        ),
        sa.CheckConstraint(
            "phase IN ('prepared', 'file_committed', 'reconciled')",
            name="ck_ppj_phase",
        ),
        sa.CheckConstraint(
            "expected_revision >= 0", name="ck_ppj_expected_revision"
        ),
        sa.CheckConstraint(
            "target_revision IS NULL OR target_revision >= 1",
            name="ck_ppj_target_revision",
        ),
        sa.CheckConstraint(
            "op <> 'upsert' OR (target_revision IS NOT NULL "
            "AND target_sha256 IS NOT NULL AND schema_revision IS NOT NULL)",
            name="ck_ppj_upsert_targets",
        ),
        sa.CheckConstraint(
            "op <> 'delete' OR (target_revision IS NULL "
            "AND target_sha256 IS NULL AND schema_revision IS NULL "
            "AND payload IS NULL)",
            name="ck_ppj_delete_targets",
        ),
        sa.CheckConstraint(
            "phase <> 'prepared' OR op <> 'upsert' OR payload IS NOT NULL",
            name="ck_ppj_upsert_payload_prepared",
        ),
        sa.CheckConstraint(
            "phase = 'prepared' OR payload IS NULL",
            name="ck_ppj_payload_only_prepared",
        ),
    )
    # One live intent per server (C05 rule 6 mirror); reconciled history is
    # exempt so retries stay idempotent and tombstones can be purged.
    op.create_index(
        "uq_ppj_one_live_intent_per_server",
        "profile_projection_journal",
        ["server_id"],
        unique=True,
        postgresql_where=sa.text(f"phase <> '{_RECONCILED}'"),
    )
    op.create_index(
        "ix_ppj_server_id",
        "profile_projection_journal",
        ["server_id"],
    )
    op.create_index(
        "ix_ppj_phase",
        "profile_projection_journal",
        ["phase"],
    )


def downgrade() -> None:
    op.drop_index("ix_ppj_phase", table_name="profile_projection_journal")
    op.drop_index("ix_ppj_server_id", table_name="profile_projection_journal")
    op.drop_index(
        "uq_ppj_one_live_intent_per_server",
        table_name="profile_projection_journal",
    )
    op.drop_table("profile_projection_journal")
    op.drop_table("profile_projection")
