"""admin credential + revocable admin sessions (card 02-auth-store)

Schema for contracts.md **C08 rule 1** and auth ADR ``3e79449`` §3–§4:

- ``admin_credential`` — the single administrator's Argon2id password hash
  (§3). One-shot bootstrap: exactly one row can ever exist (singleton
  ``id = 1``); the CHECK constraint admits only ``$argon2id$`` PHC strings,
  so no plaintext or foreign hash format can enter SQL through any writer.
- ``admin_sessions`` — server-side session records for the opaque
  ``mai_session`` token (§4). Only ``SHA-256(token)`` is stored; the frozen
  columns are ``created_at``/``last_seen_at``/``expires_at`` (absolute,
  created + 30 days), ``idle_expires_at`` (last_seen + 24 h, sliding) and
  ``revoked_at`` (set on revocation; the row is retained for audit and is
  never reactivated — no statement in the owning module clears it once set).

Chain position: second revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0001`` —
this migration serializes strictly after the P02 projection schema. DBOS
system tables stay engine-owned and out of this chain.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "admin_credential",
        sa.Column(
            "id",
            sa.SmallInteger(),
            primary_key=True,
            server_default=sa.text("1"),
            comment="Singleton row: exactly one administrator (ADR §2).",
        ),
        sa.Column(
            "password_hash",
            sa.Text(),
            nullable=False,
            comment="Argon2id PHC string (ADR §3.1); never plaintext.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC timestamp of the one-shot bootstrap (C01 rule 5).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC timestamp of the last credential (re)write.",
        ),
        sa.CheckConstraint("id = 1", name="ck_admin_credential_singleton"),
        sa.CheckConstraint(
            "password_hash LIKE '$argon2id$%'",
            name="ck_admin_credential_argon2id",
        ),
    )
    op.create_table(
        "admin_sessions",
        sa.Column(
            "token_hash",
            sa.String(length=64),
            primary_key=True,
            comment="SHA-256 of the opaque 256-bit mai_session token; "
            "the token itself is never stored (ADR §4).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC login time (absolute-expiry anchor).",
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC of the last authenticated use (idle anchor, "
            "sliding window + eviction order).",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Absolute expiry = created_at + 30 days (ADR §4).",
        ),
        sa.Column(
            "idle_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Idle expiry = last_seen_at + 24 h, refreshed on "
            "authenticated use (ADR §4).",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC revocation time; row retained for audit, never "
            "reactivated (ADR §4).",
        ),
        sa.CheckConstraint(
            "char_length(token_hash) = 64", name="ck_admin_sessions_token_hash"
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_admin_sessions_absolute_expiry"
        ),
        sa.CheckConstraint(
            "idle_expires_at > last_seen_at", name="ck_admin_sessions_idle_expiry"
        ),
    )
    # Eviction order for the ≤16 live-session cap (ADR §4): oldest
    # ``last_seen_at`` goes first.
    op.create_index(
        "ix_admin_sessions_last_seen",
        "admin_sessions",
        ["last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_admin_sessions_last_seen", table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_table("admin_credential")
