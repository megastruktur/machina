"""payload-bound approvals + one-use consumption records (card 03-approval-store)

Schema for contracts.md **C03 rule 4** (approval binding) and **C01 rule 1**
(opaque UUID identity):

- ``approvals`` — one row per human approval decision. The row binds the
  operation it authorizes: ``payload_hash`` (SHA-256 hex of the canonical
  operation payload — payload bytes never enter SQL), ``server_id`` (the
  bound target), ``capability_id`` + ``server_id`` (the bound scope: the
  exact capability application), ``policy_revision`` (the revision the
  decision was made under — any grant add/revoke or deny-list change is a
  new revision, so a revoked grant can never match) and ``expires_at``
  (exclusive expiry). Decision ownership is recorded as ``created_by``
  (granting principal); one-use consumption is recorded atomically as
  ``consumed_at``/``consumed_by`` (consuming principal). A consumed row is
  retained for audit and never un-consumed: the owning module's statements
  only ever set the consumption columns, never clear them.

  There is no stored "expired"/"revoked" state: expiry is a property of
  ``expires_at`` against the clock at use time, and grant revocation is
  denied via the ``policy_revision`` binding. The derived status is
  persisted-state only (``pending`` until ``consumed_at`` is set), so an
  audit replay of the row is clock-independent.

Chain position: third revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0002`` —
this migration serializes strictly after the P02 auth schema. DBOS system
tables stay engine-owned and out of this chain.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column(
            "approval_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque approval identity (C01 rule 1); consumption key.",
        ),
        sa.Column(
            "capability_id",
            sa.Text(),
            nullable=False,
            comment="Capability the approval authorizes — half of the C03 "
            "rule-4 scope binding (capability_id x server_id).",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Bound target server (C03 rule 4 server/target binding); "
            "second half of the scope binding.",
        ),
        sa.Column(
            "payload_hash",
            sa.Text(),
            nullable=False,
            comment="SHA-256 hex of the canonical operation payload (C03 "
            "rule 4 payload binding); payload bytes never enter SQL.",
        ),
        sa.Column(
            "policy_revision",
            sa.Integer(),
            nullable=False,
            comment="Policy revision the decision was made under (C03 rule "
            "4 revision binding; 03-policy-config bumps it on every "
            "grant/deny-list change, so a revoked grant never matches).",
        ),
        sa.Column(
            "created_by",
            sa.Text(),
            nullable=False,
            comment="Principal that granted the decision (decision "
            "ownership, C01 rule 2); from the authenticated session only.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC grant time.",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Expiry binding (C03 rule 4); exclusive — at "
            "expires_at exactly, the approval is dead.",
        ),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC one-use consumption; NULL while pending. Set "
            "atomically by the owning module, never cleared.",
        ),
        sa.Column(
            "consumed_by",
            sa.Text(),
            nullable=True,
            comment="Principal that consumed the decision; NULL while "
            "pending. Set atomically with consumed_at.",
        ),
        sa.CheckConstraint(
            "capability_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'",
            name="ck_approvals_capability_slug",
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_approvals_payload_hash",
        ),
        sa.CheckConstraint(
            "policy_revision >= 1", name="ck_approvals_policy_revision"
        ),
        sa.CheckConstraint(
            "char_length(created_by) BETWEEN 1 AND 256",
            name="ck_approvals_created_by",
        ),
        sa.CheckConstraint(
            "consumed_by IS NULL OR char_length(consumed_by) BETWEEN 1 AND 256",
            name="ck_approvals_consumed_by",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL) = (consumed_by IS NULL)",
            name="ck_approvals_consumption_pair",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_approvals_expiry_after_creation"
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="ck_approvals_consumption_monotonic",
        ),
    )


def downgrade() -> None:
    op.drop_table("approvals")
