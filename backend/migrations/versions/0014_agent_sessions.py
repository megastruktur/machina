"""Session-first schema: expand tables, clean reset, destructive DDL prepared

Schema for the ``machina-session-first-chat`` campaign task **D02** against
``sfc_PLAN.md`` D02 and the D01 contract vocabulary
(:mod:`mai.contracts.sessions`):

- ``sessions`` — one row per chat session: the stable identity and the only
  authoritative domain history root (sfc_INTENT §1). ``server_id`` is the
  explicit scope key (C01 rule 7); ``source``/``status`` pin the D01
  vocabularies at the schema level.
- ``session_entries`` — the durable domain log. ``(session_id, seq)`` UNIQUE
  keeps the per-session sequence monotonic and total (the store allocates it
  under the session row lock). ``(run_id, durable_source_id, kind)`` UNIQUE
  is the plan's idempotency key ``(run_id, source_id, kind)``: a replayed
  durable source re-reads the committed entry instead of duplicating it.
  PostgreSQL treats NULLs as distinct in unique indexes, so this key
  idempotency applies to RUN-BOUND entries (the trusted producers of user,
  assistant, tool-result and run-state traffic); session-scoped entries
  without a run (compaction/control markers) deduplicate through their own
  producer-side stable ``durable_source_id`` + store read-back instead.
- ``session_runs`` — one row per subordinate child execution (sfc_INTENT §1:
  a run never becomes a conversation identity). ``run_id <> session_id`` is a
  schema invariant, mirroring the D01 identity conflict.
- ``session_admissions`` — admission idempotency ledger:
  ``client_request_id`` is UNIQUE — one admission per client request; a
  retry returns the committed admission row (no second run, no second
  side effect).
- ``session_provider_artifacts`` — provider reasoning/thinking artifacts
  isolated per ``(provider, model_profile)`` compatibility identity
  (sfc_RESEARCH §2: explicit safe artifacts only; raw chain-of-thought is
  never authoritative history). Content is bounded and ``redacted``
  honored; artifacts are read only by the model-history layer, never
  exposed as public reasoning.
- ``session_tags`` — optional per-session tag association sharing the
  RETAINED generic ``tags`` catalog (0011); append-only, no triggers (the
  0011 trigger machinery stays with ``tags``/``job_tags``/
  ``conversation_tags``).

**Clean reset (executed by ``upgrade()``).** The user confirmed old agent
messages are disposable test data: no converter, no mandatory backup. The
reset deletes agent-owned rows from the RETAINED generic tables in FK-safe
order. The discriminator is deterministic and defensible: a run belongs to
the removed agent writers (``agent.run`` / ``agent.run_v2`` /
``agent.delegate``) exactly when it is referenced by the obsolete identity
tables — ``conversation_runs.run_id`` (every chat run attaches to its
conversation, written in the admission transaction by
``mai.agents.conversation_store``) or ``objective_runs.run_id`` /
``objective_runs.parent_run_id`` (written by ``mai.agents.objective_store``
and ``mai.agents.delegate``) — plus the delegation-child closure over
``job_runs.parent_run_id``. Unrelated generic runs (service jobs, schedules,
runbook executions through the generic submit seam) share none of those
references and survive. Deletion order: dependents first
(``objective_runs`` [FK, no ondelete action], ``job_schedule_fires`` [FK],
``job_tags``, ``job_steps``, ``job_events``), then ``job_runs``.
``job_schedules``, ``job_schedule_fires`` of unrelated runs,
``server_profiles``, ``admin_sessions``, ``approvals``, ``knowledge_*``,
``observation_*``, ``notification_*`` (alerts keep their rows; the
``run_id`` FK is ``ON DELETE SET NULL``), the ``tags`` catalog with the
reserved ``unclassified`` row and the terminal seed tags, and
``application_*``/``profile_projection*`` all survive untouched.

**Destructive DDL is prepared, NOT installed (COORD-D02-01).** The obsolete
chat/task/objective tables still have live consumers until D09, so
:func:`run_destructive_ddl` (the FK-safe DROP list) is authored and proven
here against a real disposable Postgres by the focused tests — it is NOT
wired into ``upgrade()``/``downgrade()``. It is installed at the coordinated
deletion slice (D09) as the next linear revision together with the module
deletions.

``upgrade()`` is re-runnable-safe: a partially applied upgrade can be
retried — the new tables are dropped (child-first, ``CASCADE``) before
they are recreated.

Chain position: fourteenth revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0013``.
DBOS system tables stay engine-owned and out of this chain.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-05

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None

# D01 vocabularies mirrored at the schema level (the focused tests assert
# migration and contract stay identical, so a vocabulary drift fails loudly).
_SESSION_SOURCES = ("interactive", "runbook", "document_attach", "api")
_SESSION_STATUSES = ("idle", "running", "waiting_approval", "failed", "cancelled")
_RUN_STATES = (
    "queued",
    "running",
    "waiting_approval",
    "waiting_child",
    "succeeded",
    "failed",
    "cancelled",
    "outcome_unknown",
)
_ENTRY_KINDS = (
    "user_message",
    "assistant_message",
    "tool_result",
    "run_state",
    "delegation",
    "control",
    "compaction",
)
_ARTIFACT_KINDS = ("summary", "signature", "opaque_ref")
_ASSIGNMENT_SOURCES = ("agent", "user", "system")

_SOURCES_SQL = ", ".join(f"'{v}'" for v in _SESSION_SOURCES)
_STATUSES_SQL = ", ".join(f"'{v}'" for v in _SESSION_STATUSES)
_RUN_STATES_SQL = ", ".join(f"'{v}'" for v in _RUN_STATES)
_ENTRY_KINDS_SQL = ", ".join(f"'{v}'" for v in _ENTRY_KINDS)
_ARTIFACT_KINDS_SQL = ", ".join(f"'{v}'" for v in _ARTIFACT_KINDS)
_ASSIGNMENT_SOURCES_SQL = ", ".join(f"'{v}'" for v in _ASSIGNMENT_SOURCES)

#: New tables, child-first, for the re-runnable guard and the downgrade.
_NEW_TABLES_CHILD_FIRST = (
    "session_tags",
    "session_provider_artifacts",
    "session_admissions",
    "session_runs",
    "session_entries",
    "sessions",
)

# --- clean reset (generic-table agent history; COORD-D02-01) --------------------

#: The reset runs inside the migration transaction: the seed set is a temp
#: table (``ON COMMIT DROP``), the closure converges before any DELETE, and
#: an aborted migration rolls the whole reset back (no half reset).
_CLEAN_RESET_SQL = """
DO $reset$
DECLARE
    changed integer := 1;
BEGIN
    -- Seed: run ids the obsolete identity/journal tables reference.
    CREATE TEMP TABLE _sfc_agent_run_ids (run_id uuid PRIMARY KEY)
        ON COMMIT DROP;
    INSERT INTO _sfc_agent_run_ids (run_id)
        SELECT run_id FROM public.conversation_runs
        UNION
        SELECT run_id FROM public.objective_runs
        UNION
        SELECT parent_run_id FROM public.objective_runs
        WHERE parent_run_id IS NOT NULL;

    -- Closure: delegation children (agent.delegate history) of agent roots.
    WHILE changed > 0 LOOP
        INSERT INTO _sfc_agent_run_ids (run_id)
            SELECT r.run_id
            FROM public.job_runs r
            JOIN _sfc_agent_run_ids s ON r.parent_run_id = s.run_id
            WHERE NOT EXISTS (
                SELECT 1 FROM _sfc_agent_run_ids t WHERE t.run_id = r.run_id
            );
        GET DIAGNOSTICS changed = ROW_COUNT;
    END LOOP;

    -- Dependents first (FK-safe; the 0011 mandatory-tag trigger allows an
    -- association to go away when the entity row itself is gone, and both
    -- happen inside THIS transaction before commit).
    DELETE FROM public.objective_runs r
        USING _sfc_agent_run_ids s
        WHERE r.run_id = s.run_id OR r.parent_run_id = s.run_id;
    DELETE FROM public.job_schedule_fires f
        USING _sfc_agent_run_ids s
        WHERE f.run_id = s.run_id;
    DELETE FROM public.job_tags t
        USING _sfc_agent_run_ids s
        WHERE t.job_id = s.run_id;
    DELETE FROM public.job_steps st
        USING _sfc_agent_run_ids s
        WHERE st.run_id = s.run_id;
    DELETE FROM public.job_events e
        USING _sfc_agent_run_ids s
        WHERE e.run_id = s.run_id;
    DELETE FROM public.job_runs r
        USING _sfc_agent_run_ids s
        WHERE r.run_id = s.run_id;
END
$reset$;
"""

# --- destructive DDL (authored + proven here; INSTALLED AT D09) -----------------

#: Obsolete chat/task/objective/runtime-journal tables, FK-safe drop order:
#: children first. ``objective_runs`` also references the RETAINED
#: ``job_runs`` (no ondelete action) so it goes before its siblings and
#: ``objectives``; ``conversation_tags`` references the RETAINED ``tags``
#: catalog (RESTRICT) so it goes before ``conversations``.
DESTRUCTIVE_DDL_STATEMENTS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS public.objective_runs",
    "DROP TABLE IF EXISTS public.objective_events",
    "DROP TABLE IF EXISTS public.objective_checklists",
    "DROP TABLE IF EXISTS public.objective_journal",
    "DROP TABLE IF EXISTS public.objective_servers",
    "DROP TABLE IF EXISTS public.objectives",
    "DROP TABLE IF EXISTS public.agent_task_completions",
    "DROP TABLE IF EXISTS public.agent_task_revisions",
    "DROP TABLE IF EXISTS public.agent_task_evidence",
    "DROP TABLE IF EXISTS public.agent_task_criteria",
    "DROP TABLE IF EXISTS public.agent_task_constraints",
    "DROP TABLE IF EXISTS public.agent_task_steps",
    "DROP TABLE IF EXISTS public.agent_tasks",
    "DROP TABLE IF EXISTS public.conversation_tags",
    "DROP TABLE IF EXISTS public.conversation_steering",
    "DROP TABLE IF EXISTS public.conversation_runs",
    "DROP TABLE IF EXISTS public.conversation_messages",
    "DROP TABLE IF EXISTS public.conversations",
)


def run_destructive_ddl(connection: sa.engine.Connection) -> None:
    """Execute the destructive drop list on one open connection.

    Campaign ``machina-session-first-chat``: proven by
    ``backend/tests/test_session_migration.py`` against a real disposable
    Postgres (obsolete tables absent, generic surface intact and queryable,
    no dangling FK). **Installed at the coordinated deletion slice (D09)** —
    NOT wired into this revision's ``upgrade()``/``downgrade()`` because the
    retained chat/task/objective modules still consume those tables until
    the coordinated code deletion (COORD-D02-01).
    """
    for statement in DESTRUCTIVE_DDL_STATEMENTS:
        connection.exec_driver_sql(statement)


def upgrade() -> None:
    # 1. Clean reset: agent-owned rows out of the RETAINED generic tables.
    op.execute(_CLEAN_RESET_SQL)

    # 2. Re-runnable guard: a partially applied upgrade can be retried.
    for table in _NEW_TABLES_CHILD_FIRST:
        op.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE")

    # 3. Expand: the session-first tables (FK-safe creation order).
    op.create_table(
        "sessions",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Stable session identity (C01 rule 1); the only "
            "conversation/history root (sfc_INTENT §1).",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Server scope pinning this session (C01 rule 7).",
        ),
        sa.Column(
            "title",
            sa.Text(),
            nullable=True,
            comment="Optional user-assigned session title.",
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            comment="How the session came into existence (D01 "
            "SessionSourceKind vocabulary).",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="Coarse session status projected from its runs (D01 "
            "SessionStatus vocabulary).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC creation time (C01 rule 5).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC last-change time; session-list ordering key.",
        ),
        sa.CheckConstraint(
            f"source IN ({_SOURCES_SQL})", name="ck_sessions_source"
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUSES_SQL})", name="ck_sessions_status"
        ),
        sa.CheckConstraint(
            "title IS NULL OR char_length(title) BETWEEN 1 AND 240",
            name="ck_sessions_title",
        ),
    )
    op.create_index(
        "ix_sessions_server_updated",
        "sessions",
        ["server_id", sa.text("updated_at DESC"), "session_id"],
    )
    op.create_index("ix_sessions_server_id", "sessions", ["server_id"])

    op.create_table(
        "session_entries",
        sa.Column(
            "entry_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Stable domain-entry identity (C01 rule 1).",
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning session (the only history root); ON DELETE CASCADE.",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Subordinate run that produced this entry, when run-scoped.",
        ),
        sa.Column(
            "seq",
            sa.BigInteger(),
            nullable=False,
            comment="Per-session monotonic sequence, allocated under the "
            "session row lock; UNIQUE per session.",
        ),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            comment="Discriminated entry kind (D01 EntryKind vocabulary).",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            comment="D01 SessionEntryPayload document (discriminated on `kind`).",
        ),
        sa.Column(
            "durable_source_id",
            sa.Text(),
            nullable=False,
            comment="Stable producer source id backing idempotent re-appends.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC commit time (C01 rule 5).",
        ),
        sa.CheckConstraint("seq >= 1", name="ck_session_entries_seq"),
        sa.CheckConstraint(
            f"kind IN ({_ENTRY_KINDS_SQL})", name="ck_session_entries_kind"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_session_entries_payload_object",
        ),
        sa.CheckConstraint(
            "char_length(durable_source_id) BETWEEN 1 AND 128",
            name="ck_session_entries_source_id",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
            name="fk_session_entries_session",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "session_id", "seq", name="uq_session_entries_session_seq"
        ),
        sa.UniqueConstraint(
            "run_id",
            "durable_source_id",
            "kind",
            name="uq_session_entries_run_source_kind",
        ),
    )
    op.create_index(
        "ix_session_entries_session_seq",
        "session_entries",
        ["session_id", sa.text("seq DESC")],
    )

    op.create_table(
        "session_runs",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Subordinate run identity (never a history root).",
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning session; ON DELETE CASCADE.",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Server scope pinning this run (C01 rule 7).",
        ),
        sa.Column(
            "parent_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Parent run for delegated child executions; NULL for roots.",
        ),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            comment="Frozen lifecycle state (D01 SessionRunState vocabulary).",
        ),
        sa.Column(
            "model_profile",
            sa.Text(),
            nullable=False,
            comment="Model profile identity the run executes with.",
        ),
        sa.Column(
            "client_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Client idempotency key that admitted the run.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC creation time (C01 rule 5).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC last-transition time.",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC terminal time; NULL until the run settles.",
        ),
        sa.CheckConstraint(
            "run_id <> session_id", name="ck_session_runs_identity"
        ),
        sa.CheckConstraint(
            f"state IN ({_RUN_STATES_SQL})", name="ck_session_runs_state"
        ),
        sa.CheckConstraint(
            "char_length(model_profile) BETWEEN 1 AND 128",
            name="ck_session_runs_model_profile",
        ),
        sa.CheckConstraint(
            "parent_run_id IS NULL OR parent_run_id <> run_id",
            name="ck_session_runs_parent_distinct",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
            name="fk_session_runs_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["session_runs.run_id"],
            name="fk_session_runs_parent",
        ),
    )
    op.create_index(
        "ix_session_runs_session_created",
        "session_runs",
        ["session_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_session_runs_server_id", "session_runs", ["server_id"])

    op.create_table(
        "session_admissions",
        sa.Column(
            "admission_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Stable admission identity (C01 rule 1).",
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Session the request was admitted into; ON DELETE CASCADE.",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Run allocated by the admission, when one was created.",
        ),
        sa.Column(
            "client_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Client idempotency key; UNIQUE — one admission per request.",
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            comment="Admission source (D01 SessionSourceKind vocabulary).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC admission time (C01 rule 5).",
        ),
        sa.CheckConstraint(
            f"source IN ({_SOURCES_SQL})", name="ck_session_admissions_source"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
            name="fk_session_admissions_session",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "client_request_id", name="uq_session_admissions_client_request"
        ),
    )
    op.create_index(
        "ix_session_admissions_session_created",
        "session_admissions",
        ["session_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "session_provider_artifacts",
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="Stable artifact identity (C01 rule 1).",
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning session; ON DELETE CASCADE.",
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Run that produced the artifact, when run-scoped.",
        ),
        sa.Column(
            "provider",
            sa.Text(),
            nullable=False,
            comment="Provider/model family the artifact is compatible with.",
        ),
        sa.Column(
            "model_profile",
            sa.Text(),
            nullable=False,
            comment="Model profile identity the artifact belongs to.",
        ),
        sa.Column(
            "artifact_kind",
            sa.String(length=32),
            nullable=False,
            comment="Artifact class (D01 ProviderReasoningArtifact literal "
            "vocabulary: summary/signature/opaque_ref).",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="Bounded artifact payload or opaque reference token.",
        ),
        sa.Column(
            "redacted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="True when content was deliberately redacted.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC creation time (C01 rule 5).",
        ),
        sa.CheckConstraint(
            f"artifact_kind IN ({_ARTIFACT_KINDS_SQL})",
            name="ck_session_provider_artifacts_kind",
        ),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 20000",
            name="ck_session_provider_artifacts_content",
        ),
        sa.CheckConstraint(
            "char_length(provider) BETWEEN 1 AND 128",
            name="ck_session_provider_artifacts_provider",
        ),
        sa.CheckConstraint(
            "char_length(model_profile) BETWEEN 1 AND 128",
            name="ck_session_provider_artifacts_model_profile",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
            name="fk_session_provider_artifacts_session",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_session_provider_artifacts_session_created",
        "session_provider_artifacts",
        ["session_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_session_provider_artifacts_compatibility",
        "session_provider_artifacts",
        ["provider", "model_profile"],
    )

    op.create_table(
        "session_tags",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Tagged session; ON DELETE CASCADE.",
        ),
        sa.Column(
            "tag_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Catalog tag from the RETAINED generic tags table (0011).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="UTC association time (C01 rule 5).",
        ),
        sa.Column(
            "assigned_by",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
            comment="Actor reference for the association (bounded).",
        ),
        sa.Column(
            "assignment_source",
            sa.String(length=16),
            nullable=False,
            server_default="system",
            comment="Writer vocabulary (agent/user/system), matching 0011.",
        ),
        sa.CheckConstraint(
            f"assignment_source IN ({_ASSIGNMENT_SOURCES_SQL})",
            name="ck_session_tags_assignment_source",
        ),
        sa.CheckConstraint(
            "char_length(assigned_by) <= 200",
            name="ck_session_tags_assigned_by",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
            name="fk_session_tags_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.tag_id"],
            name="fk_session_tags_tag",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id", "tag_id", name="pk_session_tags"),
    )
    op.create_index(
        "ix_session_tags_tag_session",
        "session_tags",
        ["tag_id", "session_id"],
    )


def downgrade() -> None:
    """Drop only the new tables (child-first); restores nothing.

    The clean reset is a deliberate, user-approved data deletion: a
    downgrade does not resurrect deleted agent history, and the obsolete
    chat/task/objective tables are dropped by the coordinated D09 revision,
    never here (COORD-D02-01).
    """
    for table in _NEW_TABLES_CHILD_FIRST:
        op.drop_table(table)
