"""Runtime v2 objective/journal/checklist persistence (journal-store slice)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-01

App-owned objective-domain persistence for Runtime v2 (runtime-v2.md §3,
§4, §5, §8, §11, §14; the ONE migration owner for the objective/journal/
checklist tables — later slices consume the registered chain without
renumbering):

- ``objectives`` — the explicit goal episode: the verbatim operator request
  and operator constraints (provenance-carrying, never model-reworded),
  one status with frozen terminal states, one CAS ``revision`` (scope
  edits are exact-operator-action + expected-revision, §3.2), and the
  bounded budget caps vocabulary of ``mai.agents.budgets``. The
  ``server_id`` column pins the initiating (home) server: the C8-style
  scope key every store read fails closed against.
- ``objective_servers`` — the explicit operator-owned in-scope server set
  (§3.1/§3.2). Membership rows are the scope; the activity ledger records
  its history. The initiating server is always a member.
- ``objective_runs`` — durable run ↔ objective binding (§3.3): every v2
  run carries its objective and exactly one initiating server (a scope
  member at submit); the ``handler`` CHECK admits only ``agent.run_v2`` —
  the durable "no in-flight legacy run becomes v2" guard (§10.2).
  ``UNIQUE(run_id)`` makes the binding 1:1; ``parent_run_id`` links
  delegated children.
- ``objective_events`` — the canonical ``objective.*`` activity ledger
  (§5.1/§14): the C05 rule 4 envelope shape at objective scope
  (``event_id``/``seq``/``ts``/``type``/``v``/``data``), the §4
  ``actor_kind`` provenance, per-objective monotonic ``seq`` allocated
  under ``SELECT ... FOR UPDATE`` on the objective row with
  ``UNIQUE (objective_id, seq)`` as the backstop. Types follow the frozen
  dotted-slug grammar and the ``objective.`` family prefix (additive,
  never renamed); payloads are JSON objects projected BEFORE persistence.
- ``objective_checklists`` — the bounded advisory checklist projection
  (§8.2) with its own CAS revision; malformed writes are rejected at the
  store boundary and can never erase effects or fail a run.
- ``objective_journal`` — the derived work-journal projection cache (§8.1)
  keyed by objective: a JSON document plus the exact ledger ``derived_seq``
  cursor it was derived through, so crash/restart catch-up is honest and
  the projection can always be rebuilt by replaying the ledger.

The ledger is the authority; the journal and checklist tables are
projections consumed by it (no second execution engine). Vocabulary CHECK
literals mirror the frozen contract enums in
``mai.contracts.objectives`` (``ObjectiveStatus``, ``ActorKind``) and the
event grammar of ``mai.contracts.jobs``; the focused tests compare the
live CHECK literals against the contract enums so vocabulary drift fails
loudly.

Downgrade drops the six tables child-first (projections, then ledger,
then linkage, then scope set, then the objective row itself). Legacy
domains (conversations/tasks/runs) are untouched: legacy rows remain
facts (§10.3).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen vocabularies, mirrored from mai.contracts.objectives. The focused
# tests extract the live CHECK literals and compare them to the contract
# enums, so a vocabulary drift fails loudly.
_STATUS_SQL = "('active', 'completed', 'failed', 'cancelled')"
_ACTORS_SQL = "('operator', 'agent', 'child_agent', 'automation')"
_HANDLER_SQL = "('agent.run_v2')"

# Bounded text columns, mirrored from mai.contracts.objectives /
# mai.contracts.conversations bounds.
_TITLE_MAX = 240  # tasks.TITLE_MAX_CHARS (via contracts.objectives)
_REQUEST_MAX = 20_000  # conversations.MAX_MESSAGE_CHARS
# Corruption backstop for event payloads: the store bounds payloads at
# 32768 CHARS (STREAM_TOOL_DETAIL_MAX_CHARS) before the INSERT; the CHECK
# below is a 4x-octet belt that can only trip on out-of-band writers.
_EVENT_DATA_OCTETS_MAX = 131_072

# Dotted-slug grammar (the frozen event-type grammar of jobs.py) with the
# objective.* family prefix (runtime-v2.md §5.1: additions stay in-family).
_EVENT_TYPE_SQL = (
    "type ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]+)*$' "
    "AND type LIKE 'objective.%'"
)


def upgrade() -> None:
    op.create_table(
        "objectives",
        sa.Column(
            "objective_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque stable objective identity (§3.1; C01 opaque-UUID "
            "set); minted by the store, never a server-side default.",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Initiating (home) server (§3.3): the C8-style scope "
            "key every read fails closed against; always a member of the "
            "explicit scope set.",
        ),
        sa.Column(
            "client_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Creation idempotency key; UNIQUE per home server — a "
            "retransmitted objective creation returns the SAME episode.",
        ),
        sa.Column(
            "content_fingerprint",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="SHA-256 fingerprint of the projected creation content; "
            "same key + same fingerprint deduplicates, same key + changed "
            "fingerprint is the stable client_request_conflict.",
        ),
        sa.Column(
            "title",
            sa.Text(),
            nullable=True,
            comment="Operator-assigned title; NULL when unset.",
        ),
        sa.Column(
            "original_request",
            sa.Text(),
            nullable=False,
            comment="Verbatim operator request, SECRET-PROJECTED before "
            "this column is reached (§5.3: zero secret persistence; "
            "provenance-carrying, never model-reworded).",
        ),
        sa.Column(
            "constraints",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
            comment="Verbatim operator constraints (bounded string array); "
            "model-origin text is never authority here (§3.1).",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="active",
            comment="Objective status (§3.1): active | completed | failed "
            "| cancelled; terminal states are frozen at the store seam.",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Optimistic concurrency token (§3.2): bumped on every "
            "accepted header mutation under the objective-row lock; a "
            "stale expected_revision is the stable revision_conflict.",
        ),
        sa.Column(
            "budget",
            postgresql.JSONB(),
            nullable=True,
            comment="Operator-set budget caps (§3: bounded budgets; the "
            "caps vocabulary mirrors mai.agents.budgets bounds); NULL = "
            "unset. The budget LEDGER stays the spend authority.",
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
            comment="Last-activity time; advanced transactionally with "
            "every accepted mutation.",
        ),
        sa.CheckConstraint(
            "title IS NULL OR char_length(title) BETWEEN 1 AND %d" % _TITLE_MAX,
            name="ck_objectives_title",
        ),
        sa.CheckConstraint(
            "char_length(original_request) BETWEEN 1 AND %d" % _REQUEST_MAX,
            name="ck_objectives_original_request",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(constraints) = 'array'",
            name="ck_objectives_constraints_array",
        ),
        sa.CheckConstraint(
            "status IN %s" % _STATUS_SQL,
            name="ck_objectives_status",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_objectives_revision",
        ),
        sa.CheckConstraint(
            "budget IS NULL OR jsonb_typeof(budget) = 'object'",
            name="ck_objectives_budget_object",
        ),
        sa.UniqueConstraint(
            "server_id",
            "client_request_id",
            name="uq_objectives_client_request",
        ),
    )
    op.create_index(
        "ix_objectives_home",
        "objectives",
        ["server_id", "updated_at"],
    )

    op.create_table(
        "objective_servers",
        sa.Column(
            "objective_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning objective; ON DELETE CASCADE — scope members "
            "never outlive their objective.",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="One explicitly in-scope server (§3.1); the set of "
            "these rows IS the objective scope.",
        ),
        sa.Column(
            "added_by_actor_kind",
            sa.Text(),
            nullable=False,
            comment="Actor provenance of the admission (§4): operator | "
            "agent | child_agent | automation — only operator admissions "
            "are legal (enforced at the store seam).",
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Admission time (C01 rule 5: UTC).",
        ),
        sa.PrimaryKeyConstraint(
            "objective_id",
            "server_id",
            name="pk_objective_servers",
        ),
        sa.CheckConstraint(
            "added_by_actor_kind IN %s" % _ACTORS_SQL,
            name="ck_objective_servers_actor",
        ),
    )
    op.create_index(
        "ix_objective_servers_server",
        "objective_servers",
        ["server_id"],
    )

    op.create_table(
        "objective_runs",
        sa.Column(
            "objective_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning objective; ON DELETE CASCADE.",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="The durable engine run (C05 identity); UNIQUE — a run "
            "binds to exactly one objective (§3.3).",
        ),
        sa.Column(
            "parent_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Parent delegated run (§7 child linkage); NULL for "
            "top-level runs. Must reference a run bound to the SAME "
            "objective (enforced at the store seam).",
        ),
        sa.Column(
            "initiating_server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="The run's ONE initiating server (§3.3); must be a "
            "scope member at bind time (store-enforced, fail closed).",
        ),
        sa.Column(
            "handler",
            sa.Text(),
            nullable=False,
            comment="The run handler identity; CHECK admits only "
            "'agent.run_v2' — the durable §10.2 guard that no in-flight "
            "legacy run becomes v2.",
        ),
        sa.Column(
            "actor_kind",
            sa.Text(),
            nullable=False,
            comment="Binding actor provenance (§4).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Binding time (C01 rule 5: UTC).",
        ),
        sa.PrimaryKeyConstraint("objective_id", "run_id", name="pk_objective_runs"),
        sa.UniqueConstraint("run_id", name="uq_objective_runs_run"),
        sa.CheckConstraint(
            "handler IN %s" % _HANDLER_SQL,
            name="ck_objective_runs_handler_v2",
        ),
        sa.CheckConstraint(
            "actor_kind IN %s" % _ACTORS_SQL,
            name="ck_objective_runs_actor",
        ),
        sa.ForeignKeyConstraint(
            ["objective_id"],
            ["objectives.objective_id"],
            name="fk_objective_runs_objective",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["job_runs.run_id"],
            name="fk_objective_runs_run",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["job_runs.run_id"],
            name="fk_objective_runs_parent",
        ),
    )
    op.create_index(
        "ix_objective_runs_parent",
        "objective_runs",
        ["parent_run_id"],
    )

    op.create_table(
        "objective_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque stable event identity (C05 rule 4 envelope).",
        ),
        sa.Column(
            "objective_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning objective; ON DELETE CASCADE — ledger rows "
            "never outlive their objective.",
        ),
        sa.Column(
            "seq",
            sa.BigInteger(),
            nullable=False,
            comment="Objective-local monotonic sequence starting at 1 "
            "(§5); allocated under SELECT ... FOR UPDATE on the "
            "objective row so concurrent writers serialize.",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Producing run when the event originated inside a run "
            "(provenance only; NULL for operator/automation events).",
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="RFC3339 UTC event timestamp (C01 rule 5).",
        ),
        sa.Column(
            "type",
            sa.Text(),
            nullable=False,
            comment="Dotted-slug event type inside the objective.* family "
            "(§5.1/§14): frozen six + additive in-family additions only.",
        ),
        sa.Column(
            "actor_kind",
            sa.Text(),
            nullable=False,
            comment="Actor provenance (§4): operator | agent | child_agent "
            "| automation — labels, never principals.",
        ),
        sa.Column(
            "v",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Payload schema version (C05 rule 4 envelope).",
        ),
        sa.Column(
            "data",
            postgresql.JSONB(),
            nullable=False,
            comment="Value-free PROJECTED payload (§5.3): identities, "
            "counts and sanitized references — never task prose, never "
            "secret values. Redaction happened BEFORE this table.",
        ),
        sa.CheckConstraint(
            "seq >= 1",
            name="ck_objective_events_seq",
        ),
        sa.CheckConstraint(
            _EVENT_TYPE_SQL,
            name="ck_objective_events_type_family",
        ),
        sa.CheckConstraint(
            "actor_kind IN %s" % _ACTORS_SQL,
            name="ck_objective_events_actor",
        ),
        sa.CheckConstraint(
            "v >= 1",
            name="ck_objective_events_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(data) = 'object'",
            name="ck_objective_events_data_object",
        ),
        sa.CheckConstraint(
            "octet_length(data::text) <= %d" % _EVENT_DATA_OCTETS_MAX,
            name="ck_objective_events_data_size",
        ),
        sa.ForeignKeyConstraint(
            ["objective_id"],
            ["objectives.objective_id"],
            name="fk_objective_events_objective",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "objective_id",
            "seq",
            name="uq_objective_events_seq",
        ),
    )
    op.create_index(
        "ix_objective_events_type",
        "objective_events",
        ["objective_id", "type", "seq"],
    )

    op.create_table(
        "objective_checklists",
        sa.Column(
            "objective_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Owning objective; ON DELETE CASCADE — one checklist "
            "projection per objective (§8.2).",
        ),
        sa.Column(
            "items",
            postgresql.JSONB(),
            nullable=False,
            comment="The bounded advisory checklist (typed array of "
            "{text, done}); store-validated BEFORE the INSERT. Advisory "
            "only: no tick grants authority or completes anything.",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Checklist CAS revision (§11: revision-CAS where "
            "mutable); bumped on every accepted PUT under the objective "
            "row lock.",
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
            comment="Last-accept time (C01 rule 5: UTC).",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(items) = 'array'",
            name="ck_objective_checklists_items_array",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_objective_checklists_revision",
        ),
        sa.ForeignKeyConstraint(
            ["objective_id"],
            ["objectives.objective_id"],
            name="fk_objective_checklists_objective",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "objective_journal",
        sa.Column(
            "objective_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Owning objective; ON DELETE CASCADE — one journal "
            "projection per objective (§8.1).",
        ),
        sa.Column(
            "projection",
            postgresql.JSONB(),
            nullable=False,
            comment="The derived work-journal projection document (typed, "
            "bounded sections with explicit truncation flags); always "
            "exactly the fold of the ledger through derived_seq.",
        ),
        sa.Column(
            "derived_seq",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="The ledger cursor this projection was derived "
            "through; catch-up folds events after it, rebuild replays "
            "from zero (honest crash/restart recovery).",
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
            comment="Last-projection time (C01 rule 5: UTC).",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(projection) = 'object'",
            name="ck_objective_journal_projection_object",
        ),
        sa.CheckConstraint(
            "derived_seq >= 0",
            name="ck_objective_journal_derived_seq",
        ),
        sa.ForeignKeyConstraint(
            ["objective_id"],
            ["objectives.objective_id"],
            name="fk_objective_journal_objective",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    # Child-first order keeps the reverse chain deterministic: the two
    # projections, then the ledger, then run linkage, then the scope set,
    # then the objective rows. Legacy domains are untouched.
    op.drop_table("objective_journal")
    op.drop_table("objective_checklists")
    op.drop_index("ix_objective_events_type", table_name="objective_events")
    op.drop_table("objective_events")
    op.drop_index("ix_objective_runs_parent", table_name="objective_runs")
    op.drop_table("objective_runs")
    op.drop_index("ix_objective_servers_server", table_name="objective_servers")
    op.drop_table("objective_servers")
    op.drop_index("ix_objectives_home", table_name="objectives")
    op.drop_table("objectives")
