"""conversation + message + run-link persistence (card 04-conversation-store)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-24

App-owned conversation-domain persistence (act_CONTRACTS §1–§2, ratified):

- ``conversations`` — stable conversation identity pinned to one server
  scope (§1); carries the honest ``history_completeness`` marker plus the
  missing range when reconstructed history is not complete (§4), and the
  current active task/run references for the singleton header view (§3).
- ``conversation_messages`` — append-only, role-preserving transcript rows
  (§1): a conversation-local monotonic ``seq`` starting at 1, the trusted
  producer-assigned role and provenance, the SECRET-PROJECTED content (raw
  secrets are scrubbed through ``mai.agents.output_safety`` BEFORE this
  table is ever reached — C2: zero secret persistence), the content
  fingerprint that classifies retransmissions, and the UI-authored
  ``client_request_id`` idempotency key (UNIQUE per conversation: same key
  + same fingerprint deduplicates, same key + changed fingerprint is the
  stable ``client_request_conflict``).
- ``conversation_runs`` — durable run linkage (§1: multiple runs contribute
  to one conversation/task); the row with ``released_at IS NULL`` is the
  conversation's one writer/executor lease; task episodes are referenced,
  not re-owned (0009 owns ``agent_tasks``).
- ``conversation_steering`` — the app-owned durable turn-delivery /
  steering queue (§2/§3): deterministic pause/cancel/steer control rows
  persisted BEFORE queue notification so a crash between enqueue and
  delivery reconciles without duplicate intake (atomic enqueue recovery;
  UNIQUE ``(conversation_id, client_request_id)``).

Vocabulary CHECK literals mirror the frozen contract enums in
``mai.contracts.conversations`` (``MessageRole``, ``MessageSourceKind``,
``HistoryCompleteness``) and the steer vocabulary in the same module; the
focused tests extract the live CHECK literals and compare them to the
contract enums, so a vocabulary drift fails loudly. Text bounds mirror the
contract constants (title 240, message 20,000, content_ref 512).

Downgrade drops the four tables in child-first order (the FKs cascade, but
the explicit order keeps the reverse chain deterministic).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen vocabularies, mirrored from mai.contracts.conversations. The focused
# tests extract the live CHECK literals and compare them to the contract
# enums, so a vocabulary drift fails loudly.
_ROLES_SQL = "('user', 'assistant', 'tool', 'system')"
_SOURCES_SQL = "('browser', 'api', 'legacy_import', 'system')"
_COMPLETENESS_SQL = "('complete', 'partial', 'unavailable')"
_ACTIONS_SQL = "('steer', 'pause', 'cancel')"
_DELIVERY_SQL = "('pending', 'applied', 'cancelled')"

# Bounded text columns, mirrored from mai.contracts.conversations /
# mai.contracts.tasks bounds.
_TITLE_MAX = 240  # tasks.TITLE_MAX_CHARS
_MESSAGE_MAX = 20_000  # conversations.MAX_MESSAGE_CHARS
_CONTENT_REF_MAX = 512  # conversations.CONTENT_REF_MAX_CHARS


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Stable conversation identity (§1); minted by the "
            "store, never a server-side default, independent of the "
            "last run.",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Server scope pinning this conversation (§1 rule 2; "
            "cross-server access fails closed at the store boundary).",
        ),
        sa.Column(
            "title",
            sa.Text(),
            nullable=True,
            comment="User-assigned conversation title; NULL when unset.",
        ),
        sa.Column(
            "history_completeness",
            sa.Text(),
            nullable=False,
            server_default="complete",
            comment="Honest history marker (§4): complete | partial | "
            "unavailable — partial/unavailable history is shown, never "
            "papered over with fabricated turns.",
        ),
        sa.Column(
            "missing_from_seq",
            sa.BigInteger(),
            nullable=True,
            comment="First missing transcript sequence when history is "
            "not complete (§4); paired with missing_to_seq.",
        ),
        sa.Column(
            "missing_to_seq",
            sa.BigInteger(),
            nullable=True,
            comment="Last missing transcript sequence, inclusive; paired "
            "with missing_from_seq (both NULL or both present, "
            "to >= from).",
        ),
        sa.Column(
            "active_task_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Current active task episode (§1); NULL = no task "
            "episode. The task rows themselves belong to 0009 — this is "
            "a reference, never a re-owned row.",
        ),
        sa.Column(
            "active_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Current durable run attached to the conversation "
            "(§3 singleton header); NULL = no attached run.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Creation time (C01 rule 5: UTC).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last-activity time; advanced transactionally when a "
            "message is appended (drives the list-page ordering).",
        ),
        sa.CheckConstraint(
            "title IS NULL OR char_length(title) BETWEEN 1 AND %d"
            % _TITLE_MAX,
            name="ck_conversations_title",
        ),
        sa.CheckConstraint(
            "history_completeness IN %s" % _COMPLETENESS_SQL,
            name="ck_conversations_history_completeness",
        ),
        sa.CheckConstraint(
            "(missing_from_seq IS NULL) = (missing_to_seq IS NULL)",
            name="ck_conversations_missing_range_pair",
        ),
        sa.CheckConstraint(
            "missing_from_seq IS NULL OR (missing_from_seq >= 1 AND "
            "missing_to_seq >= missing_from_seq)",
            name="ck_conversations_missing_range_ordered",
        ),
        sa.CheckConstraint(
            "history_completeness <> 'complete' OR (missing_from_seq IS "
            "NULL AND missing_to_seq IS NULL)",
            name="ck_conversations_complete_has_no_gap",
        ),
    )
    op.create_index(
        "ix_conversations_server",
        "conversations",
        ["server_id", "updated_at"],
    )

    op.create_table(
        "conversation_messages",
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque stable message identity (§1); minted by the "
            "store, never a server-side default.",
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning conversation; ON DELETE CASCADE — transcript "
            "rows never outlive their conversation.",
        ),
        sa.Column(
            "seq",
            sa.BigInteger(),
            nullable=False,
            comment="Conversation-local monotonic sequence starting at 1 "
            "(§1); allocated under SELECT ... FOR UPDATE on the "
            "conversation row so concurrent submissions serialize.",
        ),
        sa.Column(
            "role",
            sa.Text(),
            nullable=False,
            comment="Trusted producer-assigned role (§1): user | "
            "assistant | tool | system — no writable role field exists "
            "on any intake payload, so roles cannot be forged.",
        ),
        sa.Column(
            "source_kind",
            sa.Text(),
            nullable=False,
            comment="Origin of the message (§1/§4): browser | api | "
            "legacy_import | system.",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="SECRET-PROJECTED message text (C2: zero secret "
            "persistence — scrubbed through output_safety before this "
            "column is ever reached). Deliberately redacted rows make "
            "no textual-fidelity claim.",
        ),
        sa.Column(
            "content_fingerprint",
            sa.Text(),
            nullable=False,
            comment="SHA-256 fingerprint of the PROJECTED content; the "
            "retransmission classifier compares fingerprints, so "
            "idempotency is defined over stored truth.",
        ),
        sa.Column(
            "redacted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="True when content was deliberately redacted (no "
            "fidelity claim).",
        ),
        sa.Column(
            "content_ref",
            sa.Text(),
            nullable=True,
            comment="Safe out-of-band content reference instead of raw "
            "text (C05 privacy resolution); NULL when carried inline.",
        ),
        sa.Column(
            "client_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="UI-authored idempotency key (§1); UNIQUE per "
            "conversation. NULL for producer-created rows (assistant/"
            "tool/system), which are at-most-once by construction.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="RFC3339 UTC message timestamp (C01 rule 5).",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            name="fk_conversation_messages_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "seq >= 1",
            name="ck_conversation_messages_seq",
        ),
        sa.CheckConstraint(
            "role IN %s" % _ROLES_SQL,
            name="ck_conversation_messages_role",
        ),
        sa.CheckConstraint(
            "source_kind IN %s" % _SOURCES_SQL,
            name="ck_conversation_messages_source",
        ),
        sa.CheckConstraint(
            "char_length(content) <= %d" % _MESSAGE_MAX,
            name="ck_conversation_messages_content",
        ),
        sa.CheckConstraint(
            "char_length(content_fingerprint) = 64",
            name="ck_conversation_messages_fingerprint",
        ),
        sa.CheckConstraint(
            "content_ref IS NULL OR char_length(content_ref) BETWEEN 1 "
            "AND %d" % _CONTENT_REF_MAX,
            name="ck_conversation_messages_content_ref",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "seq",
            name="uq_conversation_messages_seq",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "client_request_id",
            name="uq_conversation_messages_client_request",
        ),
    )

    op.create_table(
        "conversation_runs",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning conversation; ON DELETE CASCADE.",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Durable run attempt (§1); DBOS checkpoints own the "
            "execution history — this is the app-owned linkage row only.",
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Task episode the run contributes to (§1); NULL when "
            "the run is not bound to a task episode.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Run attach time (C01 rule 5: UTC).",
        ),
        sa.Column(
            "released_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Lease release time; NULL = this run holds the "
            "conversation's one writer/executor lease (§1).",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            name="fk_conversation_runs_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "conversation_id",
            "run_id",
            name="pk_conversation_runs",
        ),
    )
    op.create_index(
        "ix_conversation_runs_active",
        "conversation_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )

    op.create_table(
        "conversation_steering",
        sa.Column(
            "steering_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque steering/control identity; minted by the "
            "store, never a server-side default.",
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Target conversation; ON DELETE CASCADE.",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Target run, when the control addresses a specific "
            "active run; NULL = conversation-scoped control.",
        ),
        sa.Column(
            "client_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Idempotency key for repeated control submissions "
            "(§3); UNIQUE per conversation — a crash between enqueue and "
            "delivery replays onto the SAME durable row.",
        ),
        sa.Column(
            "action",
            sa.Text(),
            nullable=False,
            comment="Deterministic control action (§3): steer | pause | "
            "cancel — pause/cancel never wait for a planner.",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=True,
            comment="SECRET-PROJECTED steering text (C2); required for "
            "steer, forbidden on pause/cancel.",
        ),
        sa.Column(
            "delivery_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
            comment="Durable turn-delivery state (§2): pending | "
            "applied | cancelled — persisted BEFORE queue notification "
            "so enqueue recovery is atomic.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Enqueue time (C01 rule 5: UTC).",
        ),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Time the control was applied at a safe boundary; "
            "NULL while pending (UI displays pending/applied distinctly).",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            name="fk_conversation_steering_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "action IN %s" % _ACTIONS_SQL,
            name="ck_conversation_steering_action",
        ),
        sa.CheckConstraint(
            "delivery_status IN %s" % _DELIVERY_SQL,
            name="ck_conversation_steering_delivery",
        ),
        sa.CheckConstraint(
            "content IS NULL OR char_length(content) BETWEEN 1 AND %d"
            % _MESSAGE_MAX,
            name="ck_conversation_steering_content",
        ),
        sa.CheckConstraint(
            "(action = 'steer') = (content IS NOT NULL)",
            name="ck_conversation_steering_action_content_pair",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "client_request_id",
            name="uq_conversation_steering_client_request",
        ),
    )
    op.create_index(
        "ix_conversation_steering_pending",
        "conversation_steering",
        ["conversation_id", "created_at"],
        postgresql_where=sa.text("delivery_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_steering_pending",
        table_name="conversation_steering",
    )
    op.drop_table("conversation_steering")
    op.drop_index(
        "ix_conversation_runs_active",
        table_name="conversation_runs",
    )
    op.drop_table("conversation_runs")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_server", table_name="conversations")
    op.drop_table("conversations")
