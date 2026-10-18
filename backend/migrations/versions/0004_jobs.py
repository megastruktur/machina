"""runs, steps, events and schedules domain schema (card 05-job-schema)

Schema for contracts.md **C05** (runs, steps, schedules and shared locks) and
the job-engine ADR (`bc8d264`, DBOS `dbos==3.2.0`):

- ``job_runs`` — one row per domain run. ``run_id`` is the opaque domain
  identity (C01 rule 1) AND the deterministic workflow ID, so the primary
  key is the frozen submit-intent dedup (C05 rule 5: duplicate submits with
  the same ID attach to one run; a lost ID race surfaces a conflict at the
  facade, never a second row). The row records the stable identity the
  contract requires: ``server_id`` target, ``parent_run_id`` + ``depth``
  (delegation tree, C05 rules 3/7), ``profile_revision``/``prompt_revision``
  (stable revisions), ``model_profile``, ``requested_grants`` (grant
  references — secrets never enter persisted payloads), ``root_budget``
  (frozen-at-submit root budget object; caps vocabulary owned by
  06-delegation-budgets), ``error_ref``/``evidence_ref`` (references, never
  payloads). ``cancel_requested_at`` is the facade-recorded cancellation
  request (C05 rule 2: requested ≠ verified). The table deliberately has NO
  engine-status column: DBOS system tables are the canonical run-state owner
  (job-engine ADR "run-state ownership") — no duplicate run-state authority.
- ``job_steps`` — one row per durable step intent + safe receipt
  (05-steps acceptance 1). ``state`` CHECK pins the frozen seven-value
  vocabulary from ``mai.contracts.jobs.StepState`` (the C04 rule 4
  ``CommandStatus`` outcomes plus pre-dispatch ``pending``);
  ``outcome_unknown`` is persisted exactly when a receipt is missing or the
  effect is unproven (job-engine guard rule 2 — never auto-finalized).
  ``(run_id, seq)`` is UNIQUE: per-run step order is durable and total.
- ``job_events`` — the browser-facing event projection (C05 rule 4: the
  stream is a view of persisted events). Envelope columns mirror the frozen
  wire shape; ``v`` >= 1 pins payload versioning; ``(run_id, seq)`` UNIQUE
  keeps the per-run cursor monotonic and replayable (05-run-lifecycle
  acceptance 1; 05-jobs-api cursor replay; EVT-REPLAY scenario).
- ``job_schedules`` — the facade-owned schedule record (job-engine ADR
  responsibility split: schedule management is facade surface; the engine's
  cron trigger is derived from this row). ``cron_timezone`` is explicit,
  default UTC (engine rule 7, C01 rule 5); ``misfire_policy`` is pinned to
  the single frozen value ``skip_missed`` (C05 rule 8: skip missed fires,
  resume from now, no catch-up); ``run_spec`` is the run intent template
  applied per fire (references only, never secrets).
- ``job_schedule_fires`` — dispatch ledger with PRIMARY KEY
  ``(schedule_id, planned_fire_time)``: the storage-level dedup required by
  C05 rule 8 (the engine additionally dedups per fire time by deterministic
  workflow ID — spike exp5). ``run_id`` links the fire to the run created
  for it once submitted.

No locks table: mutation locks (C05 rule 6) are owned by ``05-resource-locks``
and do not persist domain rows in this chain.

Chain position: fourth revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0003`` —
this migration serializes strictly after the P03 approval schema. DBOS
system tables stay engine-owned and out of this chain (``env.py``); they are
created by the engine at launch, never here.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

# StepState vocabulary (C04 rule 4 CommandStatus outcomes + pending). The
# vocabulary is mirrored from mai.contracts.jobs; the focused tests assert
# migration and contract stay identical, so a vocabulary drift fails loudly.
_STEP_STATES = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "outcome_unknown",
    "cancel_requested",
    "cancelled",
)

_MISFIRE_POLICIES = ("skip_missed",)

_STEP_STATES_SQL = ", ".join(f"'{state}'" for state in _STEP_STATES)
_MISFIRE_POLICIES_SQL = ", ".join(f"'{policy}'" for policy in _MISFIRE_POLICIES)


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque run identity (C01 rule 1); deterministic workflow "
            "ID and submit-intent dedup key (C05 rule 5).",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Target server (C05 rule 3); scope enforcement key (C01 rule 7).",
        ),
        sa.Column(
            "parent_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Parent delegation run (C05 rules 3/7); NULL for roots.",
        ),
        sa.Column(
            "depth",
            sa.Integer(),
            nullable=False,
            comment="Tree depth: 0 for roots, >= 1 under a parent (C05 rule 3).",
        ),
        sa.Column(
            "profile_revision",
            sa.Integer(),
            nullable=False,
            comment="Target profile revision bound at submit (C05 rule 3).",
        ),
        sa.Column(
            "prompt_revision",
            sa.Integer(),
            nullable=False,
            comment="Prompt revision bound at submit (C05 rule 3, C08 rule 5).",
        ),
        sa.Column(
            "model_profile",
            sa.Text(),
            nullable=False,
            comment="Selected model profile name (C05 rule 3).",
        ),
        sa.Column(
            "requested_grants",
            postgresql.JSONB(),
            nullable=False,
            comment="Requested grant references (C05 rule 3); references "
            "only — secrets never enter persisted payloads.",
        ),
        sa.Column(
            "root_budget",
            postgresql.JSONB(),
            nullable=True,
            comment="Root budget object frozen at submit (C05 rules 3/7; "
            "caps vocabulary owned by 06-delegation-budgets); NULL = "
            "unbudgeted root.",
        ),
        sa.Column(
            "error_ref",
            sa.Text(),
            nullable=True,
            comment="Error reference (C05 rule 3); a reference, never a payload.",
        ),
        sa.Column(
            "evidence_ref",
            sa.Text(),
            nullable=True,
            comment="Evidence reference (C05 rule 3); a reference, never a payload.",
        ),
        sa.Column(
            "cancel_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Facade-recorded cancellation request (C05 rule 2: "
            "requested ≠ verified until the engine reports CANCELLED).",
        ),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC submit time (C01 rule 5).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC last-update time (C01 rule 5).",
        ),
        sa.CheckConstraint(
            "parent_run_id IS NOT NULL OR depth = 0", name="ck_job_runs_root_depth"
        ),
        sa.CheckConstraint(
            "parent_run_id IS NULL OR depth >= 1", name="ck_job_runs_child_depth"
        ),
        sa.CheckConstraint("depth >= 0", name="ck_job_runs_depth"),
        sa.CheckConstraint(
            "profile_revision >= 1", name="ck_job_runs_profile_revision"
        ),
        sa.CheckConstraint("prompt_revision >= 1", name="ck_job_runs_prompt_revision"),
        sa.CheckConstraint(
            "char_length(model_profile) BETWEEN 1 AND 200",
            name="ck_job_runs_model_profile",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(requested_grants) = 'array'",
            name="ck_job_runs_grants_array",
        ),
        sa.CheckConstraint(
            "root_budget IS NULL OR jsonb_typeof(root_budget) = 'object'",
            name="ck_job_runs_budget_object",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"], ["job_runs.run_id"], name="fk_job_runs_parent"
        ),
    )
    op.create_index("ix_job_runs_server_id", "job_runs", ["server_id"])
    op.create_index("ix_job_runs_parent_run_id", "job_runs", ["parent_run_id"])
    op.create_table(
        "job_steps",
        sa.Column(
            "step_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque step identity (C01 rule 1).",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning run (05-steps: the durable run owns step intent).",
        ),
        sa.Column(
            "seq",
            sa.Integer(),
            nullable=False,
            comment="Per-run step order; durable and total with the UNIQUE key.",
        ),
        sa.Column(
            "capability_id",
            sa.Text(),
            nullable=False,
            comment="Capability executing this step (C03 rule 1 identifier).",
        ),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
            comment="Frozen StepState vocabulary (C04 rule 4 outcomes + "
            "pending); guard rule 2: outcome_unknown is never auto-finalized.",
        ),
        sa.Column(
            "receipt",
            postgresql.JSONB(),
            nullable=True,
            comment="Persisted safe receipt (C04 rule 4: value-free by "
            "construction); NULL until a receipt exists.",
        ),
        sa.CheckConstraint("seq >= 1", name="ck_job_steps_seq"),
        sa.CheckConstraint(
            "char_length(capability_id) BETWEEN 1 AND 200",
            name="ck_job_steps_capability",
        ),
        sa.CheckConstraint(
            f"state IN ({_STEP_STATES_SQL})",
            name="ck_job_steps_state",
        ),
        sa.CheckConstraint(
            "receipt IS NULL OR jsonb_typeof(receipt) = 'object'",
            name="ck_job_steps_receipt_object",
        ),
        sa.CheckConstraint(
            "state <> 'pending' OR receipt IS NULL",
            name="ck_job_steps_pending_no_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["job_runs.run_id"], name="fk_job_steps_run"
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_job_steps_run_seq"),
    )
    op.create_table(
        "job_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque event identity (C01 rule 1).",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Run this event belongs to (C05 rule 4).",
        ),
        sa.Column(
            "seq",
            sa.Integer(),
            nullable=False,
            comment="Monotonic per-run cursor (C05 rule 4); UNIQUE with run_id.",
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Event time, stored UTC (C01 rule 5).",
        ),
        sa.Column(
            "type",
            sa.Text(),
            nullable=False,
            comment="Lowercase dotted event slug (e.g. step.completed); the "
            "envelope shape is frozen, the type vocabulary stays open.",
        ),
        sa.Column(
            "v",
            sa.Integer(),
            nullable=False,
            comment="Payload schema version (C05 rule 4: non-versioned "
            "payloads are bad).",
        ),
        sa.Column(
            "data",
            postgresql.JSONB(),
            nullable=False,
            comment="Versioned payload object; value-free by producer "
            "contract (C05 rule 4: payloads never carry secret values).",
        ),
        sa.CheckConstraint("seq >= 1", name="ck_job_events_seq"),
        sa.CheckConstraint("v >= 1", name="ck_job_events_v"),
        sa.CheckConstraint(
            "char_length(type) BETWEEN 1 AND 100", name="ck_job_events_type_len"
        ),
        sa.CheckConstraint(
            "type ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]+)*$'",
            name="ck_job_events_type_slug",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["job_runs.run_id"], name="fk_job_events_run"
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_job_events_run_seq"),
    )
    op.create_table(
        "job_schedules",
        sa.Column(
            "schedule_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque schedule identity (C01 rule 1).",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Bound target server (05-schedules-api acceptance 1; "
            "C01 rule 7 scope).",
        ),
        sa.Column(
            "cron",
            sa.Text(),
            nullable=False,
            comment="Recurrence expression; syntax is engine-validated at "
            "registration (engine rule 7: time-of-day logic lives in the "
            "scheduled workflow).",
        ),
        sa.Column(
            "cron_timezone",
            sa.Text(),
            nullable=False,
            server_default="UTC",
            comment="Explicit IANA timezone (engine rule 7; C01 rule 5), default UTC.",
        ),
        sa.Column(
            "misfire_policy",
            sa.Text(),
            nullable=False,
            server_default="skip_missed",
            comment="Frozen misfire policy (C05 rule 8): skip missed fires, "
            "resume from now, no catch-up — single legal value.",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Paused schedules stay persisted but fire nothing "
            "(05-schedules acceptance 2).",
        ),
        sa.Column(
            "run_spec",
            postgresql.JSONB(),
            nullable=False,
            comment="Run intent template applied per fire; references only "
            "(C05 rule 3 fields), never secrets.",
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
            "char_length(cron) BETWEEN 1 AND 200", name="ck_job_schedules_cron"
        ),
        sa.CheckConstraint(
            "char_length(cron_timezone) BETWEEN 1 AND 100",
            name="ck_job_schedules_tz_len",
        ),
        sa.CheckConstraint(
            f"misfire_policy IN ({_MISFIRE_POLICIES_SQL})",
            name="ck_job_schedules_misfire",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(run_spec) = 'object'", name="ck_job_schedules_spec_object"
        ),
    )
    op.create_index("ix_job_schedules_server_id", "job_schedules", ["server_id"])
    op.create_table(
        "job_schedule_fires",
        sa.Column(
            "schedule_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Fired schedule; half of the dedup key (C05 rule 8).",
        ),
        sa.Column(
            "planned_fire_time",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Planned fire time; other half of the dedup key (C05 rule 8).",
        ),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC dispatch record time (C01 rule 5).",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Run created for this fire; NULL until submitted.",
        ),
        sa.PrimaryKeyConstraint(
            "schedule_id", "planned_fire_time", name="pk_job_schedule_fires"
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["job_schedules.schedule_id"],
            name="fk_job_schedule_fires_schedule",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["job_runs.run_id"], name="fk_job_schedule_fires_run"
        ),
    )


def downgrade() -> None:
    op.drop_table("job_schedule_fires")
    op.drop_index("ix_job_schedules_server_id", table_name="job_schedules")
    op.drop_table("job_schedules")
    op.drop_table("job_events")
    op.drop_table("job_steps")
    op.drop_index("ix_job_runs_parent_run_id", table_name="job_runs")
    op.drop_index("ix_job_runs_server_id", table_name="job_runs")
    op.drop_table("job_runs")
