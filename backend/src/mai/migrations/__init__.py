"""Shared migration-time DDL constants (campaign ``session-first-chat``).

The destructive chat/task/objective drop list was authored and proven in
revision ``0014`` (``backend/tests/test_session_migration.py`` against a
real disposable Postgres). Revision ``0015`` — the D09 coordinated deletion
cutover — installs the SAME list in the linear chain, so the statements live
here as the single source both revisions import. Alembic version modules
cannot import each other (the ``versions/`` directory is not a package), but
``mai`` is importable from the migration environment.
"""

from __future__ import annotations

import sqlalchemy as sa

__all__ = [
    "DESTRUCTIVE_DDL_STATEMENTS",
    "run_destructive_ddl",
]

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

    Proven by ``backend/tests/test_session_migration.py`` against a real
    disposable Postgres (obsolete tables absent, generic surface intact and
    queryable, no dangling FK). Revision ``0015`` wires it into the linear
    chain at the coordinated code deletion (COORD-D02-01).
    """
    for statement in DESTRUCTIVE_DDL_STATEMENTS:
        connection.exec_driver_sql(statement)
