"""agent task + plan/constraint/criterion/evidence/revision persistence (card 11-task-store)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-24

App-owned task-domain persistence (act_CONTRACTS §1–§2/§5–§9, ratified):
task headers/revisions and the todo/constraint/evidence/outcome pages are
persisted INDEPENDENTLY of execution attempts (``conversation_runs`` links
runs; it never re-owns task state).

- ``agent_tasks`` — one task header per goal episode (§1): opaque stable
  ``task_id``, the optimistic ``revision`` CAS token, the ≤240-char title,
  the ≤1000-char summary, the advisory ``requested_phase`` (never
  permission, §6), the domain ``status`` (:class:`mai.contracts.tasks.
  TaskOutcome` — a layer above the frozen technical RunState) and the
  creation idempotency key (UNIQUE per conversation: V1 has ONE active
  task per conversation; a retransmitted intake replays onto the SAME
  task row instead of minting a second episode).
- ``agent_task_steps`` — stable todo items (§7): conversation-stable slug
  ``step_id`` (lowercase, ≤64 chars) that survives replanning deltas,
  frozen status/verification vocabularies, dependency list, evidence
  references. Superseded rows are NEVER deleted: they keep their stable
  ID with ``superseded_at``/``superseded_by`` set, so completed work
  cannot silently vanish through a replacement plan (P3). The partial
  UNIQUE ``(task_id, position)`` index covers active rows only and holds
  because every write serializes under the task-row lock.
- ``agent_task_constraints`` — source-anchored constraints (§5): ``origin``
  distinguishes user requirements from model inferences, so the store can
  fail closed on a model-origin supersede of a user constraint (§6, P4).
  Superseded constraints stay recorded.
- ``agent_task_criteria`` — measurable acceptance criteria with stable IDs
  (§5); verification is a separate field bound to evidence (§7).
- ``agent_task_evidence`` — the task-scoped evidence registry: every
  evidence reference a patch/update/verdict carries is existence-checked
  against its real source table and recorded here, giving the paginated
  evidence page and the ``known_evidence_ids`` set for verdict checks.
- ``agent_task_revisions`` — append-only revision log (§8): every accepted
  mutation bumps the header revision, records author/kind/payload and the
  caller's ``idempotency_key`` (UNIQUE per task: retransmitted mutations
  replay onto the SAME revision instead of double-applying, C4).
- ``agent_task_completions`` — completion verdict receipts (§9): the full
  validated verdict is stored verbatim; a verdict can never self-grant
  verified success (``completion_missing_evidence`` otherwise).

Vocabulary CHECK literals mirror the frozen contract enums in
``mai.contracts.tasks`` (``Phase``, ``StepStatus``, ``VerificationStatus``,
``TaskOutcome``, ``ActionRisk``, ``ConstraintOrigin``, ``EvidenceKind``);
the focused tests extract the live CHECK literals and compare them to the
contract enums, so a vocabulary drift fails loudly. Text bounds mirror the
contract constants (title 240, summary 1000, statement 500, note 200,
result summary 1000) and the stable-ID regex is enforced as a CHECK.

Downgrade drops the seven tables in child-first order (the FKs cascade,
but the explicit order keeps the reverse chain deterministic).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen vocabularies, mirrored from mai.contracts.tasks. The focused tests
# extract the live CHECK literals and compare them to the contract enums, so
# a vocabulary drift fails loudly.
_PHASES_SQL = "('plan', 'inspect', 'act')"
_OUTCOMES_SQL = (
    "('in_progress', 'needs_input', 'partially_verified', "
    "'verified', 'failed', 'cancelled')"
)
_STEP_STATUS_SQL = "('pending', 'in_progress', 'blocked', 'executed', 'skipped')"
_VERIFICATION_SQL = "('not_checked', 'passed', 'failed', 'unavailable')"
_RISK_SQL = "('read_only', 'mutating', 'irreversible')"
_ORIGIN_SQL = "('user', 'model')"
_EVIDENCE_KIND_SQL = (
    "('message', 'observation', 'run_event', 'artifact', 'document')"
)
_REVISION_KIND_SQL = (
    "('create', 'plan_patch', 'item_update', 'metadata', 'completion')"
)
_AUTHOR_SQL = "('user', 'model', 'system')"

# Bounded text columns, mirrored from mai.contracts.tasks constants.
_TITLE_MAX = 240  # tasks.TITLE_MAX_CHARS
_SUMMARY_MAX = 1000  # tasks.SUMMARY_MAX_CHARS
_STATEMENT_MAX = 500  # AcceptanceCriterion/Constraint/expected_check/skip bound
_NOTE_MAX = 200  # EvidenceRef.note bound
_RESULT_SUMMARY_MAX = 1000  # tasks.SUMMARY_MAX_CHARS (safe result summary)
_EVIDENCE_REFS_MAX = 16  # tasks.MAX_EVIDENCE_REFS
_DEPENDENCIES_MAX = 8  # tasks.MAX_DEPENDENCIES
_SOURCE_REFS_MAX = 4  # ConstraintProposal.source_message_refs bound

_STABLE_ID_SQL = "^[a-z][a-z0-9_]{0,63}$"  # tasks.STABLE_ID_PATTERN


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque stable task identity (§1); minted by the "
            "store, never a server-side default, stable across runs.",
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning conversation; ON DELETE CASCADE. V1 keeps ONE "
            "active task per conversation (§1).",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Server scope pinning this task (§1; cross-server "
            "access fails closed at the store boundary).",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Optimistic concurrency token (§1); bumped on every "
            "accepted mutation under the task-row lock. A stale "
            "expected_revision is the stable revision_conflict, never "
            "last-writer-wins (C6).",
        ),
        sa.Column(
            "title",
            sa.Text(),
            nullable=False,
            comment="Task header title (§8 GET task shape; ≤ 240 chars).",
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
            comment="Current goal statement (≤ 1000 chars); a replacement "
            "summary never removes recorded items/constraints (P3).",
        ),
        sa.Column(
            "requested_phase",
            sa.Text(),
            nullable=False,
            server_default="plan",
            comment="Advisory phase (§5/§6): plan | inspect | act — "
            "never permission; metadata updates are legal in all phases.",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="in_progress",
            comment="Domain outcome layer (§9): in_progress | "
            "needs_input | partially_verified | verified | failed | "
            "cancelled; terminal states are frozen at the store seam.",
        ),
        sa.Column(
            "active_step_id",
            sa.Text(),
            nullable=True,
            comment="Step currently being worked on, if any (§8); "
            "automatically cleared when that step executes/skips.",
        ),
        sa.Column(
            "client_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Creation idempotency key (§1); UNIQUE per "
            "conversation — a retransmitted intake returns the SAME "
            "task episode (C4). NULL for store-internal creations.",
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
            comment="Last-accept time; advanced transactionally with "
            "every accepted revision (drives list-page ordering).",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            name="fk_agent_tasks_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_agent_tasks_revision",
        ),
        sa.CheckConstraint(
            "title IS NOT NULL AND char_length(title) BETWEEN 1 AND %d"
            % _TITLE_MAX,
            name="ck_agent_tasks_title",
        ),
        sa.CheckConstraint(
            "summary IS NULL OR char_length(summary) BETWEEN 1 AND %d"
            % _SUMMARY_MAX,
            name="ck_agent_tasks_summary",
        ),
        sa.CheckConstraint(
            "requested_phase IN %s" % _PHASES_SQL,
            name="ck_agent_tasks_phase",
        ),
        sa.CheckConstraint(
            "status IN %s" % _OUTCOMES_SQL,
            name="ck_agent_tasks_status",
        ),
        sa.CheckConstraint(
            "active_step_id ~ '%s'" % _STABLE_ID_SQL,
            name="ck_agent_tasks_active_step_id",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "client_request_id",
            name="uq_agent_tasks_client_request",
        ),
    )
    op.create_index(
        "ix_agent_tasks_server",
        "agent_tasks",
        ["server_id", "updated_at"],
    )
    op.create_index(
        "ix_agent_tasks_conversation",
        "agent_tasks",
        ["conversation_id"],
    )

    op.create_table(
        "agent_task_steps",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning task; ON DELETE CASCADE.",
        ),
        sa.Column(
            "step_id",
            sa.Text(),
            nullable=False,
            comment="Stable item ID (§7): lowercase slug, stable across "
            "replanning deltas so a continuation can reference earlier "
            "items; never reused or silently dropped (P3).",
        ),
        sa.Column(
            "title",
            sa.Text(),
            nullable=False,
            comment="Short step title (§7; ≤ 240 chars).",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
            comment="Frozen todo status (§7): pending | in_progress | "
            "blocked | executed | skipped. Executed/skipped are frozen: "
            "completed work is recorded, never reopened.",
        ),
        sa.Column(
            "verification",
            sa.Text(),
            nullable=False,
            server_default="not_checked",
            comment="Separate verification state bound to evidence (§7): "
            "not_checked | passed | failed | unavailable. A pass is "
            "never silently cleared.",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            comment="Active-plan order (§8 user reorder); compacted to a "
            "dense 0..n-1 range over active rows under the task lock.",
        ),
        sa.Column(
            "risk",
            sa.Text(),
            nullable=False,
            server_default="read_only",
            comment="Declared risk/action class (§5): read_only | "
            "mutating | irreversible.",
        ),
        sa.Column(
            "prerequisite_unresolved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="True when a required prerequisite is still missing "
            "(§5; P2 records the discovery step instead of inventing "
            "facts).",
        ),
        sa.Column(
            "expected_check",
            sa.Text(),
            nullable=True,
            comment="The check that will decide this step is done "
            "(≤ 500 chars); 'done' renders only from agreed checks (§7).",
        ),
        sa.Column(
            "skip_reason",
            sa.Text(),
            nullable=True,
            comment="Required when status is skipped (≤ 500 chars); a "
            "skip never satisfies an acceptance criterion (§7).",
        ),
        sa.Column(
            "result_summary",
            sa.Text(),
            nullable=True,
            comment="Safe result summary written by task.update_item "
            "(§7; ≤ 1000 chars, no secret material).",
        ),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Sourced evidence references (EvidenceRef list, "
            "≤ 16); executed/passed state REQUIRES non-empty refs — "
            "no ticked booleans (§7, P4).",
        ),
        sa.Column(
            "depends_on",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Stable step IDs this step depends on (≤ 8); the "
            "resulting graph is cycle-checked at the store seam (§7).",
        ),
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When a replan replaced this step; NULL = active "
            "plan item. Superseded rows stay recorded (P3).",
        ),
        sa.Column(
            "superseded_by",
            sa.Text(),
            nullable=True,
            comment="Stable ID of the replacing step; paired with "
            "superseded_at.",
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
            comment="Last-accept time for this item.",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.task_id"],
            name="fk_agent_task_steps_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_id",
            "step_id",
            name="pk_agent_task_steps",
        ),
        sa.CheckConstraint(
            "step_id ~ '%s'" % _STABLE_ID_SQL,
            name="ck_agent_task_steps_step_id",
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND %d" % _TITLE_MAX,
            name="ck_agent_task_steps_title",
        ),
        sa.CheckConstraint(
            "status IN %s" % _STEP_STATUS_SQL,
            name="ck_agent_task_steps_status",
        ),
        sa.CheckConstraint(
            "verification IN %s" % _VERIFICATION_SQL,
            name="ck_agent_task_steps_verification",
        ),
        sa.CheckConstraint(
            "risk IN %s" % _RISK_SQL,
            name="ck_agent_task_steps_risk",
        ),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_agent_task_steps_position",
        ),
        sa.CheckConstraint(
            "expected_check IS NULL OR char_length(expected_check) "
            "BETWEEN 1 AND %d" % _STATEMENT_MAX,
            name="ck_agent_task_steps_expected_check",
        ),
        sa.CheckConstraint(
            "skip_reason IS NULL OR char_length(skip_reason) BETWEEN 1 "
            "AND %d" % _STATEMENT_MAX,
            name="ck_agent_task_steps_skip_reason",
        ),
        sa.CheckConstraint(
            "result_summary IS NULL OR char_length(result_summary) "
            "BETWEEN 1 AND %d" % _RESULT_SUMMARY_MAX,
            name="ck_agent_task_steps_result_summary",
        ),
        sa.CheckConstraint(
            "status <> 'executed' OR jsonb_array_length(evidence_refs) > 0",
            name="ck_agent_task_steps_executed_needs_evidence",
        ),
        sa.CheckConstraint(
            "status <> 'skipped' OR skip_reason IS NOT NULL",
            name="ck_agent_task_steps_skipped_needs_reason",
        ),
        sa.CheckConstraint(
            "verification NOT IN ('passed', 'failed') OR "
            "jsonb_array_length(evidence_refs) > 0",
            name="ck_agent_task_steps_verdict_needs_evidence",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(evidence_refs) <= %d" % _EVIDENCE_REFS_MAX,
            name="ck_agent_task_steps_evidence_refs",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(depends_on) <= %d" % _DEPENDENCIES_MAX,
            name="ck_agent_task_steps_dependencies",
        ),
        sa.CheckConstraint(
            "(superseded_at IS NULL) = (superseded_by IS NULL)",
            name="ck_agent_task_steps_supersede_pair",
        ),
    )
    op.create_index(
        "ix_agent_task_steps_active",
        "agent_task_steps",
        ["task_id", "position"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "ix_agent_task_steps_page",
        "agent_task_steps",
        ["task_id", "position", "step_id"],
    )

    op.create_table(
        "agent_task_constraints",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning task; ON DELETE CASCADE.",
        ),
        sa.Column(
            "constraint_id",
            sa.Text(),
            nullable=False,
            comment="Stable constraint slug (§5); never reused.",
        ),
        sa.Column(
            "category",
            sa.Text(),
            nullable=False,
            comment="Category slug, e.g. safety or scope (1..64 chars).",
        ),
        sa.Column(
            "statement",
            sa.Text(),
            nullable=False,
            comment="Safe constraint statement (§5; 1..500 chars, no "
            "secret material).",
        ),
        sa.Column(
            "origin",
            sa.Text(),
            nullable=False,
            comment="Source anchor (§5): user | model. A model-origin "
            "update can never supersede a user-origin constraint (§6, "
            "P4) — that needs an explicit user correction.",
        ),
        sa.Column(
            "source_message_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="User messages this constraint is anchored to "
            "(≤ 4 UUIDs); existence is checked at the store seam.",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            comment="Active-constraint order (dense over active rows).",
        ),
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When a correction replaced this constraint; NULL = "
            "active. Superseded constraints stay recorded (P4).",
        ),
        sa.Column(
            "superseded_by",
            sa.Text(),
            nullable=True,
            comment="Stable ID of the replacing constraint; paired with "
            "superseded_at.",
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
            comment="Last-accept time for this constraint.",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.task_id"],
            name="fk_agent_task_constraints_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_id",
            "constraint_id",
            name="pk_agent_task_constraints",
        ),
        sa.CheckConstraint(
            "constraint_id ~ '%s'" % _STABLE_ID_SQL,
            name="ck_agent_task_constraints_id",
        ),
        sa.CheckConstraint(
            "char_length(category) BETWEEN 1 AND 64",
            name="ck_agent_task_constraints_category",
        ),
        sa.CheckConstraint(
            "char_length(statement) BETWEEN 1 AND %d" % _STATEMENT_MAX,
            name="ck_agent_task_constraints_statement",
        ),
        sa.CheckConstraint(
            "origin IN %s" % _ORIGIN_SQL,
            name="ck_agent_task_constraints_origin",
        ),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_agent_task_constraints_position",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(source_message_refs) <= %d"
            % _SOURCE_REFS_MAX,
            name="ck_agent_task_constraints_source_refs",
        ),
        sa.CheckConstraint(
            "(superseded_at IS NULL) = (superseded_by IS NULL)",
            name="ck_agent_task_constraints_supersede_pair",
        ),
    )
    op.create_index(
        "ix_agent_task_constraints_page",
        "agent_task_constraints",
        ["task_id", "position", "constraint_id"],
    )

    op.create_table(
        "agent_task_criteria",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning task; ON DELETE CASCADE.",
        ),
        sa.Column(
            "criterion_id",
            sa.Text(),
            nullable=False,
            comment="Stable criterion slug referenced by completion "
            "verdicts (§5/§9).",
        ),
        sa.Column(
            "statement",
            sa.Text(),
            nullable=False,
            comment="Measurable statement of what counts as done "
            "(1..500 chars).",
        ),
        sa.Column(
            "verification",
            sa.Text(),
            nullable=False,
            server_default="not_checked",
            comment="Current verification state bound to evidence (§7); "
            "advanced only by validated completion verdicts.",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            comment="Stable criteria order.",
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
            comment="Last verification-advance time.",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.task_id"],
            name="fk_agent_task_criteria_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_id",
            "criterion_id",
            name="pk_agent_task_criteria",
        ),
        sa.CheckConstraint(
            "criterion_id ~ '%s'" % _STABLE_ID_SQL,
            name="ck_agent_task_criteria_id",
        ),
        sa.CheckConstraint(
            "char_length(statement) BETWEEN 1 AND %d" % _STATEMENT_MAX,
            name="ck_agent_task_criteria_statement",
        ),
        sa.CheckConstraint(
            "verification IN %s" % _VERIFICATION_SQL,
            name="ck_agent_task_criteria_verification",
        ),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_agent_task_criteria_position",
        ),
    )
    op.create_index(
        "ix_agent_task_criteria_page",
        "agent_task_criteria",
        ["task_id", "position", "criterion_id"],
    )

    op.create_table(
        "agent_task_evidence",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning task; ON DELETE CASCADE.",
        ),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Opaque ID of the sourced record (§5/§9); existence "
            "is checked against its real source table before this row "
            "is ever written (P4: hallucinated refs fail closed).",
        ),
        sa.Column(
            "kind",
            sa.Text(),
            nullable=False,
            comment="Kind of sourced record (§5): message | observation "
            "| run_event | artifact | document.",
        ),
        sa.Column(
            "note",
            sa.Text(),
            nullable=True,
            comment="Short safe annotation for UI display (≤ 200 "
            "chars); first registration wins.",
        ),
        sa.Column(
            "origin_step_id",
            sa.Text(),
            nullable=True,
            comment="Step that first referenced this evidence, when "
            "known.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="First-registration time (C01 rule 5: UTC).",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.task_id"],
            name="fk_agent_task_evidence_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_id",
            "evidence_id",
            name="pk_agent_task_evidence",
        ),
        sa.CheckConstraint(
            "kind IN %s" % _EVIDENCE_KIND_SQL,
            name="ck_agent_task_evidence_kind",
        ),
        sa.CheckConstraint(
            "note IS NULL OR char_length(note) BETWEEN 1 AND %d"
            % _NOTE_MAX,
            name="ck_agent_task_evidence_note",
        ),
    )
    op.create_index(
        "ix_agent_task_evidence_page",
        "agent_task_evidence",
        ["task_id", "created_at", "evidence_id"],
    )

    op.create_table(
        "agent_task_revisions",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning task; ON DELETE CASCADE.",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            comment="The header revision this mutation created (§1); "
            "dense 1..n sequence allocated under the task-row lock.",
        ),
        sa.Column(
            "kind",
            sa.Text(),
            nullable=False,
            comment="Mutation kind: create | plan_patch | item_update | "
            "metadata | completion.",
        ),
        sa.Column(
            "author",
            sa.Text(),
            nullable=False,
            comment="Authority of the caller (§6): user | model | "
            "system; authority checks branch on this at the seam.",
        ),
        sa.Column(
            "idempotency_key",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Caller key so retransmitted mutations deduplicate "
            "(§1); UNIQUE per task — C4.",
        ),
        sa.Column(
            "request_fingerprint",
            sa.Text(),
            nullable=True,
            comment="SHA-256 over the canonical request payload; a same-"
            "key retransmit with a different fingerprint is the stable "
            "idempotency conflict, never a silent double-apply (C4).",
        ),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="User message this mutation was produced from "
            "(§5); existence is checked at the store seam (P4).",
        ),
        sa.Column(
            "item_id",
            sa.Text(),
            nullable=True,
            comment="Stable item ID for item_update revisions.",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Applied payload receipt (bounded patch/op/verdict "
            "dump; §8 audit).",
        ),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When the mutation was applied under the lock "
            "(P5: applied revisions are distinct from pending "
            "corrections).",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.task_id"],
            name="fk_agent_task_revisions_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_id",
            "revision",
            name="pk_agent_task_revisions",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_agent_task_revisions_revision",
        ),
        sa.CheckConstraint(
            "kind IN %s" % _REVISION_KIND_SQL,
            name="ck_agent_task_revisions_kind",
        ),
        sa.CheckConstraint(
            "author IN %s" % _AUTHOR_SQL,
            name="ck_agent_task_revisions_author",
        ),
        sa.CheckConstraint(
            "request_fingerprint IS NULL OR "
            "char_length(request_fingerprint) = 64",
            name="ck_agent_task_revisions_fingerprint",
        ),
        sa.UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_agent_task_revisions_idempotency",
        ),
    )

    op.create_table(
        "agent_task_completions",
        sa.Column(
            "completion_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Opaque completion receipt identity; minted by the "
            "store, never a server-side default.",
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning task; ON DELETE CASCADE.",
        ),
        sa.Column(
            "base_revision",
            sa.Integer(),
            nullable=False,
            comment="Task revision this verdict was produced against "
            "(§9); CAS-checked at the store seam.",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            comment="The header revision this verdict created.",
        ),
        sa.Column(
            "idempotency_key",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Caller key so retransmitted verdicts deduplicate "
            "(§9); UNIQUE per task.",
        ),
        sa.Column(
            "task_outcome",
            sa.Text(),
            nullable=False,
            comment="Recorded domain outcome (§9); verified requires "
            "full criterion coverage with evidence — never self-granted.",
        ),
        sa.Column(
            "verdict",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The full validated TaskCompletionVerdict, stored "
            "verbatim as the §9 receipt.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Receipt time (C01 rule 5: UTC).",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_tasks.task_id"],
            name="fk_agent_task_completions_task",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "base_revision >= 1",
            name="ck_agent_task_completions_base_revision",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_agent_task_completions_revision",
        ),
        sa.CheckConstraint(
            "task_outcome IN %s" % _OUTCOMES_SQL,
            name="ck_agent_task_completions_outcome",
        ),
        sa.UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_agent_task_completions_idempotency",
        )
    )
    op.create_index(
        "ix_agent_task_completions_page",
        "agent_task_completions",
        ["task_id", "created_at", "completion_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_task_completions_page",
        table_name="agent_task_completions",
    )
    op.drop_table("agent_task_completions")
    op.drop_table("agent_task_revisions")
    op.drop_index(
        "ix_agent_task_evidence_page",
        table_name="agent_task_evidence",
    )
    op.drop_table("agent_task_evidence")
    op.drop_index(
        "ix_agent_task_criteria_page",
        table_name="agent_task_criteria",
    )
    op.drop_table("agent_task_criteria")
    op.drop_index(
        "ix_agent_task_constraints_page",
        table_name="agent_task_constraints",
    )
    op.drop_table("agent_task_constraints")
    op.drop_index(
        "ix_agent_task_steps_page",
        table_name="agent_task_steps",
    )
    op.drop_index(
        "ix_agent_task_steps_active",
        table_name="agent_task_steps",
    )
    op.drop_table("agent_task_steps")
    op.drop_index("ix_agent_tasks_conversation", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_server", table_name="agent_tasks")
    op.drop_table("agent_tasks")
