"""D09 coordinated cutover: drop the obsolete chat/task/objective schema.

Campaign ``machina-session-first-chat`` (COORD-D02-01): the destructive DDL
authored and proven in revision ``0014`` is installed here as the next linear
revision, together with the coordinated code deletion that stops every
retained reader of these tables. The generic surface survives untouched:

- ``tags`` catalog, ``job_tags`` associations, the reserved unclassified
  row and the ``0012_terminal_tag_seed`` behavior are PRESERVED;
- the retained ``trg_job_runs_mandatory_tag`` / ``trg_job_tags_mandatory_tag``
  constraint triggers and ``trg_tags_protect_reserved`` keep protecting the
  generic domain;
- the conversation-only trigger functions
  (``mai_history_tags_conversation_has_tag`` /
  ``mai_history_tags_conversation_tag_left``) are dropped — their tables
  (``conversations`` / ``conversation_tags``) are gone with this revision,
  so the functions are unreachable dead DDL.

Dropped tables (child-first FK-safe order, from
:data:`mai`'s proven ``DESTRUCTIVE_DDL_STATEMENTS``): ``objective_journal``,
``objective_checklists``, ``objective_events``, ``objective_runs``,
``objective_servers``, ``objectives``; ``agent_task_completions``,
``agent_task_revisions``, ``agent_task_evidence``, ``agent_task_criteria``,
``agent_task_constraints``, ``agent_task_steps``, ``agent_tasks``;
``conversation_tags``, ``conversation_steering``, ``conversation_runs``,
``conversation_messages``, ``conversations``.

Downgrade is intentionally a no-op: the old campaign data is disposable test
data (sfc_INTENT), no converter and no mandatory backup exist, and recreating
empty obsolete tables would lie about a runtime that no longer exists.
"""

from __future__ import annotations

from alembic import op

from mai.migrations import DESTRUCTIVE_DDL_STATEMENTS

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None

#: Conversation-only history-tags trigger functions (0011). The table-level
#: triggers die with their tables; the FUNCTIONS survive a table drop and
#: must be dropped explicitly. The generic functions
#: (``mai_history_tags_job_run_has_tag``, ``mai_history_tags_job_tag_left``,
#: ``mai_history_tags_protect_reserved_tag``) and their triggers stay.
_CONVERSATION_TRIGGER_FUNCTIONS: tuple[str, ...] = (
    "DROP FUNCTION IF EXISTS public.mai_history_tags_conversation_has_tag() CASCADE",
    "DROP FUNCTION IF EXISTS public.mai_history_tags_conversation_tag_left() CASCADE",
)


def upgrade() -> None:
    """Execute the proven destructive drop list, then the function cleanup."""
    connection = op.get_bind()
    for statement in (*DESTRUCTIVE_DDL_STATEMENTS, *_CONVERSATION_TRIGGER_FUNCTIONS):
        connection.exec_driver_sql(statement)


def downgrade() -> None:
    """No-op: the obsolete schema is not restorable and not missed.

    Revision 0014's ``downgrade`` removes the session-first tables; running
    it after this revision leaves a pre-session schema WITHOUT the dropped
    chat/task/objective tables. That is the honest post-cutover truth — the
    deleted runtime's tables are never fabricated empty.
    """
