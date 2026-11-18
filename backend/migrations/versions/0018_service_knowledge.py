"""service knowledge facts, history and operation receipt tables (knowledge-migration, task ask_10)

One ADDITIVE migration for the agent-service-knowledge feature
(campaign contracts C4/D3/D4/D5; bounds and vocabularies are the FROZEN
constants of ``mai.applications.knowledge_contracts``):

- ``service_knowledge_identity`` — one row per
  ``(server_id, application_id)``: the identity generation fence
  (``generation``; a pending learn/adopt with a previous generation can
  no longer apply, an explicit successful readoption creates a NEW
  generation and old learned facts are NOT inherited), the service and
  knowledge revisions, the evidenced map-only manual-identity flag and
  the no-auto-adopt tombstone (``auto_adopt_suppressed`` persists until
  an explicit operator readopt, NOT merely until the next discovery).
  Deliberately NO foreign key to ``application_projection``: server
  profiles are files, not rows (0007 convention) and an evidenced
  map-only manual identity must be representable without forcing an
  adoption-projection rewrite (C4).
- ``service_learned_fact`` — the durable learned fact: bounded category
  (1..64), semantic key (1..80), statement (1..800; NO transcript/blob
  column exists), the compare-and-set revision (>= 1; same-key divergent
  changes are a CONFLICT, never last-writer-wins), the frozen status and
  source-kind vocabularies, the BOUNDED provenance reference (1..256 —
  raw tool output/transcript never lands here), the trusted runtime
  provenance ids, the DB-owned ``verified_at``, the correction link and
  the bounded conflict reason (operator-conflicting facts stay
  ``conflict``, not effective guidance). A PARTIAL UNIQUE index over
  ``(server_id, application_id, generation, category, key)
  WHERE status = 'active'`` enforces the unique active semantic key per
  identity (unrelated keys merge; identical value with new evidence is
  idempotent — no duplicate active row).
- ``service_learned_fact_revision`` — the append-only fact history: one
  row per revision of one fact (UNIQUE ``(fact_id, revision)``), FK to
  the fact with ON DELETE CASCADE, bounded historical statement and the
  frozen vocabularies. No UPDATE/DELETE path is provided by this
  migration; the store layer owns writes.
- ``service_learning_operation`` — the durable operation RECEIPT (a
  domain receipt, NOT a second queue: DBOS remains the single execution
  authority). The deterministic operation identity is derived from the
  trusted run + persisted tool-call id, so
  ``uq_service_learning_operation_source
  (source_run_id, source_tool_call_id)`` is the dedup arbiter and
  ``uq_service_learning_operation_workflow UNIQUE (workflow_id)`` pins
  the deterministic DBOS workflow identity. ``payload_digest`` is the
  accepted SHA-256 payload hash (64 lowercase hex); ``sub_effects`` is
  the per-effect status map whose values are CHECK-pinned to the frozen
  ``applied/failed/unknown/pending`` vocabulary (``partial`` and
  ``unknown`` are DISTINCT, never collapsed).

The bounds mirror ``mai.applications.knowledge_contracts``
(``MAX_FACT_STATEMENT_CHARS``, ``MAX_FACT_KEY_CHARS``,
``MAX_SOURCE_REF_CHARS``); the focused tests extract the live CHECK
definitions and compare them to the contract constants, so drift fails
loudly (0007 discipline).

``downgrade('0017')`` reverses in dependency order
(``service_learned_fact_revision`` → ``service_learned_fact`` →
``service_learning_operation`` → ``service_knowledge_identity``;
indexes drop with their tables) and is DESTRUCTIVE FOR 0018-ERA DATA:
learned facts, their history and the operation receipts are LOST. The
original map/manifest surface (``application_projection``,
``application_projection_journal``, canonical files) is untouched by
the downgrade.

Chain position: eighteenth revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0017``.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-18

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen vocabularies, mirrored from mai.applications.knowledge_contracts
# (0007 discipline: the focused tests extract the live CHECK literals and
# compare them to the contract enums, so a vocabulary drift fails loudly).
_FACT_STATUS_SQL = "('active', 'superseded', 'retracted', 'conflict')"
_SOURCE_KIND_SQL = "('tool_result', 'user_statement')"
_OPERATION_STATUS_SQL = (
    "('accepted', 'queued', 'applying', 'applied', 'failed', 'partial', 'unknown')"
)
_SUB_EFFECT_SQL = "('applied', 'failed', 'unknown', 'pending')"

# Bounded text columns, mirrored from
# mai.applications.knowledge_contracts.
_STATEMENT_MAX = 800
_KEY_MAX = 80
_CATEGORY_MAX = 64
_SOURCE_REF_MAX = 256
_CONFLICT_REASON_MAX = 400
_TOOL_CALL_MAX = 256
_ACTOR_MAX = 256
_WORKFLOW_ID_MAX = 256
_COALESCE_KEY_MAX = 256

# The per-effect vocabulary CHECK for the ``sub_effects`` JSONB map: the
# map must be a JSON object and EVERY VALUE must be one of the frozen
# sub-effect states (keys are effect names). Subquery-free so the
# expression is a valid table CHECK (jsonb_path_query_array is IMMUTABLE;
# ``<@`` containment covers duplicate/empty cases: an empty map or a map
# with no values trivially satisfies it).
_SUB_EFFECTS_VOCABULARY_SQL = (
    "jsonb_typeof(sub_effects) = 'object' AND "
    "(jsonb_path_query_array(sub_effects, '$.*') "
    "<@ '[\"applied\", \"failed\", \"unknown\", \"pending\"]'::jsonb)"
)


def upgrade() -> None:
    """Create the four service-knowledge tables additively (no canonical
    map/manifest surface is touched)."""
    # 1. Identity + generation fence + suppression tombstone.
    op.create_table(
        "service_knowledge_identity",
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Authorized server identity (C01 rule 7)",
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Canonical application identity of the service",
        ),
        sa.Column(
            "generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="Identity generation fence (D4): stale work with a "
            "previous generation can no longer apply; explicit successful "
            "readoption creates a NEW generation without inheriting old "
            "facts.",
        ),
        sa.Column(
            "service_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Observed service revision at the last knowledge write",
        ),
        sa.Column(
            "knowledge_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Monotone knowledge revision for bounded reads",
        ),
        sa.Column(
            "manual_identity",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Evidenced map-only manual identity (D4); never a "
            "fallback for unsupported standalone containers",
        ),
        sa.Column(
            "auto_adopt_suppressed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="No-auto-adopt tombstone (D4): persists until an "
            "explicit operator readopt, not merely until the next discovery",
        ),
        sa.Column(
            "suppressed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
            comment="When auto-adoption was suppressed",
        ),
        sa.Column(
            "suppressed_generation",
            sa.Integer(),
            nullable=True,
            comment="Generation at suppression time",
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "generation >= 1", name="ck_service_knowledge_identity_generation"
        ),
        sa.CheckConstraint(
            "service_revision >= 0",
            name="ck_service_knowledge_identity_service_revision",
        ),
        sa.CheckConstraint(
            "knowledge_revision >= 0",
            name="ck_service_knowledge_identity_knowledge_revision",
        ),
        sa.CheckConstraint(
            "suppressed_generation IS NULL OR suppressed_generation >= 1",
            name="ck_service_knowledge_identity_suppressed_generation",
        ),
        sa.PrimaryKeyConstraint(
            "server_id", "application_id", name="pk_service_knowledge_identity"
        ),
        comment="Service knowledge identity + generation fence + "
        "no-auto-adopt tombstone, one row per (server_id, application_id); "
        "deliberately NO FK to application_projection (0007 convention: "
        "server profiles are files, not rows — an evidenced map-only "
        "manual identity is representable without an adoption-projection "
        "rewrite, C4)",
    )

    # 2. The durable learned fact.
    op.create_table(
        "service_learned_fact",
        sa.Column(
            "fact_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Canonical UUID identity of the learned fact",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Authorized server identity (C01 rule 7)",
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Canonical application identity of the service",
        ),
        sa.Column(
            "generation",
            sa.Integer(),
            nullable=False,
            comment="Identity generation the fact belongs to (a new "
            "generation does not inherit old facts)",
        ),
        sa.Column(
            "category",
            sa.Text(),
            nullable=False,
            comment="Bounded category grouping the fact (1..64)",
        ),
        sa.Column(
            "key",
            sa.Text(),
            nullable=False,
            comment="Bounded semantic key identifying the fact (1..80)",
        ),
        sa.Column(
            "statement",
            sa.Text(),
            nullable=False,
            comment="Bounded learned statement (1..800); never a transcript",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="Compare-and-set revision (>= 1); same-key divergent "
            "changes are a CONFLICT, never last-writer-wins",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="Lifecycle status: active | superseded | retracted | "
            "conflict (frozen vocabulary)",
        ),
        sa.Column(
            "source_kind",
            sa.Text(),
            nullable=False,
            comment="Provenance kind: tool_result | user_statement (frozen "
            "vocabulary)",
        ),
        sa.Column(
            "source_ref",
            sa.Text(),
            nullable=False,
            comment="Bounded persisted provenance reference (1..256); never "
            "raw tool output or a transcript",
        ),
        sa.Column(
            "source_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Trusted run identity that proved the fact",
        ),
        sa.Column(
            "source_session_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Owning session identity when the proof came from a session",
        ),
        sa.Column(
            "source_tool_call_id",
            sa.Text(),
            nullable=True,
            comment="Persisted tool-call identity of the proving result "
            "(1..256 when present)",
        ),
        sa.Column(
            "verified_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            comment="DB-owned verification timestamp of the source evidence",
        ),
        sa.Column(
            "supersedes_fact_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Prior fact identity this correction supersedes",
        ),
        sa.Column(
            "conflict_reason",
            sa.Text(),
            nullable=True,
            comment="Bounded reason when status = conflict (1..400); "
            "operator-conflicting facts stay conflict, not effective guidance",
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "generation >= 1", name="ck_service_learned_fact_generation"
        ),
        sa.CheckConstraint(
            "char_length(category) BETWEEN 1 AND 64",
            name="ck_service_learned_fact_category",
        ),
        sa.CheckConstraint(
            "char_length(key) BETWEEN 1 AND 80", name="ck_service_learned_fact_key"
        ),
        sa.CheckConstraint(
            "char_length(statement) BETWEEN 1 AND 800",
            name="ck_service_learned_fact_statement",
        ),
        sa.CheckConstraint(
            "revision >= 1", name="ck_service_learned_fact_revision"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'retracted', 'conflict')",
            name="ck_service_learned_fact_status",
        ),
        sa.CheckConstraint(
            "source_kind IN ('tool_result', 'user_statement')",
            name="ck_service_learned_fact_source_kind",
        ),
        sa.CheckConstraint(
            "char_length(source_ref) BETWEEN 1 AND 256",
            name="ck_service_learned_fact_source_ref",
        ),
        sa.CheckConstraint(
            "source_tool_call_id IS NULL OR char_length(source_tool_call_id) "
            "BETWEEN 1 AND 256",
            name="ck_service_learned_fact_source_tool_call_id",
        ),
        sa.CheckConstraint(
            "conflict_reason IS NULL OR char_length(conflict_reason) "
            "BETWEEN 1 AND 400",
            name="ck_service_learned_fact_conflict_reason",
        ),
        sa.PrimaryKeyConstraint("fact_id", name="pk_service_learned_fact"),
        comment="Durable learned fact for one service identity/generation "
        "(D3): bounded statement, provenance references, compare-and-set "
        "revision; unique ACTIVE semantic key per identity",
    )
    op.create_index(
        "uq_service_learned_fact_active_key",
        "service_learned_fact",
        ["server_id", "application_id", "generation", "category", "key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_service_learned_fact_scope",
        "service_learned_fact",
        ["server_id", "application_id", "generation"],
    )
    op.create_index(
        "ix_service_learned_fact_history",
        "service_learned_fact",
        ["server_id", "application_id", "created_at", "fact_id"],
    )

    # 3. Append-only fact history.
    op.create_table(
        "service_learned_fact_revision",
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Canonical UUID identity of the history row",
        ),
        sa.Column(
            "fact_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Learned fact this revision belongs to",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Authorized server identity (C01 rule 7)",
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Canonical application identity of the service",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            comment="Fact revision this historical row records (>= 1)",
        ),
        sa.Column(
            "statement",
            sa.Text(),
            nullable=False,
            comment="Historical statement value (1..800, never a transcript)",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="Lifecycle status recorded for this revision (frozen "
            "vocabulary)",
        ),
        sa.Column(
            "source_kind",
            sa.Text(),
            nullable=False,
            comment="Provenance kind recorded for this revision (frozen "
            "vocabulary)",
        ),
        sa.Column(
            "source_ref",
            sa.Text(),
            nullable=False,
            comment="Bounded persisted provenance reference (1..256)",
        ),
        sa.Column(
            "changed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "revision >= 1", name="ck_service_learned_fact_revision_revision"
        ),
        sa.CheckConstraint(
            "char_length(statement) BETWEEN 1 AND 800",
            name="ck_service_learned_fact_revision_statement",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'retracted', 'conflict')",
            name="ck_service_learned_fact_revision_status",
        ),
        sa.CheckConstraint(
            "source_kind IN ('tool_result', 'user_statement')",
            name="ck_service_learned_fact_revision_source_kind",
        ),
        sa.CheckConstraint(
            "char_length(source_ref) BETWEEN 1 AND 256",
            name="ck_service_learned_fact_revision_source_ref",
        ),
        sa.ForeignKeyConstraint(
            ["fact_id"],
            ["service_learned_fact.fact_id"],
            ondelete="CASCADE",
            name="fk_service_learned_fact_revision_fact",
        ),
        sa.PrimaryKeyConstraint("revision_id", name="pk_service_learned_fact_revision"),
        sa.UniqueConstraint(
            "fact_id", "revision", name="uq_service_learned_fact_revision_pair"
        ),
        comment="Append-only learned-fact history: one row per revision of "
        "one fact; no UPDATE/DELETE path is provided by this migration",
    )
    op.create_index(
        "ix_service_learned_fact_revision_history",
        "service_learned_fact_revision",
        ["fact_id", sa.text("revision DESC")],
    )

    # 4. Durable operation receipt (domain receipt, NOT a second queue).
    op.create_table(
        "service_learning_operation",
        sa.Column(
            "operation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Deterministic operation identity derived from run + "
            "persisted tool-call id (D5/C5)",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Authorized server identity (C01 rule 7)",
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Target service identity; NULL when the operation "
            "targets a NEW service by selector",
        ),
        sa.Column(
            "generation",
            sa.Integer(),
            nullable=True,
            comment="Identity generation fence matched before applying "
            "(NULL when the target service is new)",
        ),
        sa.Column(
            "payload_digest",
            sa.Text(),
            nullable=False,
            comment="Accepted payload hash: exactly 64 lowercase hex chars "
            "(SHA-256), mirroring OperationReceipt.payload_digest",
        ),
        sa.Column(
            "workflow_id",
            sa.Text(),
            nullable=False,
            comment="Deterministic DBOS workflow identity (1..256, UNIQUE)",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="Honest durable status: accepted | queued | applying | "
            "applied | failed | partial | unknown (partial and unknown are "
            "DISTINCT, never collapsed)",
        ),
        sa.Column(
            "sub_effects",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Per-effect durable status by effect name (adoption, "
            "map, docs, facts); values pinned to the frozen sub-effect "
            "vocabulary by CHECK",
        ),
        sa.Column(
            "coalesce_key",
            sa.Text(),
            nullable=False,
            comment="Bounded coalescing key for duplicate observations "
            "(1..256)",
        ),
        sa.Column(
            "source_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Trusted run identity that requested the operation",
        ),
        sa.Column(
            "source_tool_call_id",
            sa.Text(),
            nullable=False,
            comment="Persisted tool-call id the operation derives from (1..256)",
        ),
        sa.Column(
            "actor",
            sa.Text(),
            nullable=False,
            comment="Trusted runtime actor captured server-side (1..256); "
            "never a model-supplied identity",
        ),
        sa.Column(
            "applied_fact_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Fact ids already applied by this operation",
        ),
        sa.Column(
            "conflict_keys",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Fact keys that landed in CONFLICT during application",
        ),
        sa.Column(
            "error_code",
            sa.Text(),
            nullable=True,
            comment="Stable machine-readable error code when the operation failed",
        ),
        sa.Column(
            "error_detail",
            sa.Text(),
            nullable=True,
            comment="Value-free bounded failure detail (1..400 when present)",
        ),
        sa.Column(
            "accepted_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "generation IS NULL OR generation >= 1",
            name="ck_service_learning_operation_generation",
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_service_learning_operation_payload_digest",
        ),
        sa.CheckConstraint(
            "char_length(workflow_id) BETWEEN 1 AND 256",
            name="ck_service_learning_operation_workflow_id",
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'queued', 'applying', 'applied', "
            "'failed', 'partial', 'unknown')",
            name="ck_service_learning_operation_status",
        ),
        sa.CheckConstraint(
            _SUB_EFFECTS_VOCABULARY_SQL,
            name="ck_service_learning_operation_sub_effects_vocabulary",
        ),
        sa.CheckConstraint(
            "char_length(coalesce_key) BETWEEN 1 AND 256",
            name="ck_service_learning_operation_coalesce_key",
        ),
        sa.CheckConstraint(
            "char_length(source_tool_call_id) BETWEEN 1 AND 256",
            name="ck_service_learning_operation_source_tool_call_id",
        ),
        sa.CheckConstraint(
            "char_length(actor) BETWEEN 1 AND 256",
            name="ck_service_learning_operation_actor",
        ),
        sa.CheckConstraint(
            "error_detail IS NULL OR char_length(error_detail) BETWEEN 1 AND 400",
            name="ck_service_learning_operation_error_detail",
        ),
        sa.PrimaryKeyConstraint("operation_id", name="pk_service_learning_operation"),
        sa.UniqueConstraint(
            "source_run_id",
            "source_tool_call_id",
            name="uq_service_learning_operation_source",
        ),
        sa.UniqueConstraint(
            "workflow_id", name="uq_service_learning_operation_workflow"
        ),
        comment="Durable knowledge-operation RECEIPT (domain receipt, NOT "
        "a second queue — DBOS remains the single execution authority): "
        "deterministic identity, accepted payload hash, per-effect "
        "sub-statuses and honest status progression",
    )
    op.create_index(
        "ix_service_learning_operation_status",
        "service_learning_operation",
        ["server_id", sa.text("accepted_at DESC"), sa.text("operation_id DESC")],
    )


def downgrade() -> None:
    """Reverse in dependency order; DESTRUCTIVE FOR 0018-ERA DATA.

    Learned facts, their append-only history and the operation receipts
    are LOST. The original map/manifest surface
    (``application_projection``, ``application_projection_journal``,
    canonical files) is untouched by the downgrade. Indexes drop with
    their tables.
    """
    op.drop_table("service_learned_fact_revision")
    op.drop_table("service_learned_fact")
    op.drop_table("service_learning_operation")
    op.drop_table("service_knowledge_identity")
