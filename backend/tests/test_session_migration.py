"""Focused tests for the D02 session-first migration (0014).

Acceptance (BRIEF_D02 §A/§A.3; sfc_PLAN.md D02; COORD-D02-01):

1. ``upgrade`` from zero to head succeeds; the six new tables, their
   indexes and their named CHECK constraints exist exactly as authored.
2. ``downgrade`` one revision (0014 → 0013) drops ONLY the new tables and
   leaves every obsolete chat/task/objective table intact; a re-upgrade
   works.
3. **Clean-reset proof** (COORD-D02-01: reset executes now, destructive
   DROP deferred to D09): seed representative old data at revision 0013 —
   agent-owned runs (conversation-bound root + delegation child +
   objective-bound run, with steps/events/tags), UNRELATED generic runs, a
   schedule + fire, the tag catalog with the reserved and terminal seed
   rows — then run the real ``0014`` upgrade and assert: agent-owned rows
   are gone from the retained generic tables, unrelated generic rows are
   still present with exact counts, the tag catalog survives.
4. **Destructive-DDL proof**: run the authored :func:`run_destructive_ddl`
   helper against the real migrated database and assert every obsolete
   table is gone, the generic surface (``job_runs``/``job_steps``/
   ``job_events``/``job_tags``/``tags``/``job_schedules``) is intact and
   still queryable, and no session table was touched.

Boundary discipline: every claim runs against a real disposable PostgreSQL
fixture (``MAI_TEST_DATABASE_URL``; the chain needs the ``vector``
extension — ``pgvector/pgvector:pg16``) with the real linear Alembic chain.
Fixtures carry only fixed opaque UUIDs and synthetic text.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from mai.tag_store import RESERVED_UNCLASSIFIED_TAG_ID

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")

AGENT_CONV = uuid.UUID("b2f3d4e5-0001-4001-8001-0000000000b1")
AGENT_ROOT = uuid.UUID("a1e2c3d4-0001-4001-8001-0000000000a1")
AGENT_CHILD = uuid.UUID("a1e2c3d4-0002-4002-8002-0000000000a2")
AGENT_OBJECTIVE = uuid.UUID("d4e5f6a7-0001-4001-8001-0000000000d1")
UNRELATED_RUN = uuid.UUID("a1e2c3d4-0003-4003-8003-0000000000a3")
UNRELATED_SCHEDULE = uuid.UUID("e5f6a7b8-0001-4001-8001-0000000000e1")

NEW_TABLES = (
    "sessions",
    "session_entries",
    "session_runs",
    "session_admissions",
    "session_provider_artifacts",
    "session_tags",
)
OBSOLETE_TABLES = (
    "conversation_steering",
    "conversation_runs",
    "conversation_messages",
    "conversations",
    "conversation_tags",
    "agent_task_completions",
    "agent_task_revisions",
    "agent_task_evidence",
    "agent_task_criteria",
    "agent_task_constraints",
    "agent_task_steps",
    "agent_tasks",
    "objective_journal",
    "objective_checklists",
    "objective_events",
    "objective_runs",
    "objective_servers",
    "objectives",
)


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _set_migration_env(url: str) -> dict[str, str | None]:
    """Point ``migrations/env.py`` at the fixture URL for command calls."""
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url
    return {"previous": previous}


def _restore_migration_env(saved: dict[str, str | None]) -> None:
    if saved["previous"] is None:
        os.environ.pop("MAI_DATABASE_URL", None)
    else:
        os.environ["MAI_DATABASE_URL"] = str(saved["previous"])


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database upgraded from zero to the current head."""
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the migration claims need a real "
            "disposable Postgres fixture (pgvector/pgvector:pg16)"
        )
    saved = _set_migration_env(url)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        command.upgrade(_alembic_config(), "head")
    finally:
        _restore_migration_env(saved)
    return url


def _table_exists(url: str, table: str) -> bool:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
        ).fetchone()
    return bool(row and row[0])


def _seed_old_world(url: str) -> None:
    """Seed representative revision-0013 data: agent-owned + unrelated.

    One explicit transaction, exactly like the real writers: the 0011
    mandatory-tag triggers are DEFERRABLE INITIALLY DEFERRED, so each
    entity and its classification must share one commit.
    """
    with psycopg.connect(url) as conn:
        with conn.transaction():
            # A conversation with one agent root run and its delegation child.
            # The 0011 mandatory-tag invariant: the conversation commits with
            # its classification in the same transaction.
            conn.execute(
                """
                INSERT INTO conversations (conversation_id, server_id)
                VALUES (%s, %s)
                """,
                (AGENT_CONV, str(SERVER_A)),
            )
            conn.execute(
                """
                INSERT INTO conversation_tags
                    (conversation_id, tag_id, assignment_source)
                VALUES (%s, %s, 'system')
                """,
                (AGENT_CONV, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
            )
            conn.execute(
                """
                INSERT INTO conversation_runs (conversation_id, run_id)
                VALUES (%s, %s)
                """,
                (AGENT_CONV, AGENT_ROOT),
            )
            # The generic run rows first: objective_runs (0013) FK-references
            # job_runs, so identity rows must exist before the bindings.
            for run_id, parent, depth in (
                (AGENT_ROOT, None, 0),
                (AGENT_CHILD, AGENT_ROOT, 1),
                (UNRELATED_RUN, None, 0),
            ):
                conn.execute(
                    """
                    INSERT INTO job_runs
                        (run_id, server_id, parent_run_id, depth,
                         profile_revision, prompt_revision, model_profile,
                         requested_grants)
                    VALUES (%s, %s, %s, %s, 1, 1, %s, '[]'::jsonb)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (
                        run_id,
                        str(SERVER_A),
                        parent,
                        depth,
                        "legacy-profile" if run_id != UNRELATED_RUN else "svc-profile",
                    ),
                )
            # An objective bound to the agent root (0013 identity table).
            conn.execute(
                """
                INSERT INTO objectives (objective_id, server_id, original_request)
                VALUES (%s, %s, 'synthetic legacy request')
                """,
                (AGENT_OBJECTIVE, str(SERVER_A)),
            )
            conn.execute(
                """
                INSERT INTO objective_runs
                    (objective_id, run_id, initiating_server_id, handler,
                     actor_kind)
                VALUES (%s, %s, %s, 'agent.run_v2', 'operator')
                """,
                (AGENT_OBJECTIVE, AGENT_ROOT, str(SERVER_A)),
            )
            # Dependent rows for both agent runs and the unrelated run.
            for run_id in (AGENT_ROOT, AGENT_CHILD, UNRELATED_RUN):
                conn.execute(
                    """
                    INSERT INTO job_steps
                        (step_id, run_id, seq, capability_id)
                    VALUES (%s, %s, 1, 'host.logs')
                    """,
                    (uuid.uuid4(), run_id),
                )
                conn.execute(
                    """
                    INSERT INTO job_events
                        (event_id, run_id, seq, ts, v, type, data)
                    VALUES (%s, %s, 1, now(), 1, 'run.submitted', '{}'::jsonb)
                    """,
                    (uuid.uuid4(), run_id),
                )
                conn.execute(
                    """
                    INSERT INTO job_tags (job_id, tag_id, assignment_source)
                    VALUES (%s, %s, 'system')
                    """,
                    (run_id, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
                )
            # Objective binding for the delegation child too (parent linkage).
            conn.execute(
                """
                INSERT INTO objective_runs
                    (objective_id, run_id, parent_run_id, initiating_server_id,
                     handler, actor_kind)
                VALUES (%s, %s, %s, %s, 'agent.run_v2', 'child_agent')
                """,
                (AGENT_OBJECTIVE, AGENT_CHILD, AGENT_ROOT, str(SERVER_A)),
            )
            # An unrelated schedule whose single fire references the unrelated
            # run (the fire of a SURVIVING run must survive).
            conn.execute(
                """
                INSERT INTO job_schedules
                    (schedule_id, server_id, cron, cron_timezone, enabled,
                     run_spec)
                VALUES (%s, %s, '0 0 * * *', 'UTC', true, '{}'::jsonb)
                """,
                (UNRELATED_SCHEDULE, str(SERVER_A)),
            )
            conn.execute(
                """
                INSERT INTO job_schedule_fires
                    (schedule_id, planned_fire_time, run_id)
                VALUES (%s, '2026-01-01T00:00:00+00:00'::timestamptz, %s)
                """,
                (UNRELATED_SCHEDULE, UNRELATED_RUN),
            )


def test_upgrade_creates_tables_indexes_and_checks(migrated_url: str) -> None:
    for table in NEW_TABLES:
        assert _table_exists(migrated_url, table), table
    with psycopg.connect(migrated_url) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            ).fetchall()
        }
        checks = {
            row[0]
            for row in conn.execute(
                """
                SELECT conname FROM pg_constraint
                WHERE contype = 'c' AND conrelid::regclass::text IN (
                    'sessions', 'session_entries', 'session_runs',
                    'session_admissions', 'session_provider_artifacts',
                    'session_tags'
                )
                """
            ).fetchall()
        }
    for expected in (
        "ix_sessions_server_updated",
        "ix_session_entries_session_seq",
        "ix_session_runs_session_created",
        "ix_session_admissions_session_created",
        "ix_session_provider_artifacts_session_created",
        "ix_session_provider_artifacts_compatibility",
        "ix_session_tags_tag_session",
    ):
        assert expected in indexes, expected
    for expected in (
        "ck_sessions_source",
        "ck_sessions_status",
        "ck_sessions_title",
        "ck_session_entries_seq",
        "ck_session_entries_kind",
        "ck_session_entries_payload_object",
        "ck_session_entries_source_id",
        "ck_session_runs_identity",
        "ck_session_runs_state",
        "ck_session_runs_model_profile",
        "ck_session_runs_parent_distinct",
        "ck_session_admissions_source",
        "ck_session_provider_artifacts_kind",
        "ck_session_provider_artifacts_content",
        "ck_session_tags_assignment_source",
        "ck_session_tags_assigned_by",
    ):
        assert expected in checks, expected
    # The schema-level identity invariant exists (run_id <> session_id).
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, server_id, source, status)
            VALUES (%s, %s, 'interactive', 'idle')
            """,
            (uuid.UUID(int=0x51), str(SERVER_A)),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO session_runs
                    (run_id, session_id, server_id, state, model_profile,
                     client_request_id)
                VALUES (%s, %s, %s, 'queued', 'p', %s)
                """,
                (
                    uuid.UUID(int=0x51),  # run_id == session_id
                    uuid.UUID(int=0x51),
                    str(SERVER_A),
                    uuid.uuid4(),
                ),
            )
        conn.rollback()


def test_downgrade_drops_only_new_tables(migrated_url: str) -> None:
    saved = _set_migration_env(migrated_url)
    try:
        command.downgrade(_alembic_config(), "0013")
        for table in NEW_TABLES:
            assert not _table_exists(migrated_url, table), table
        for table in OBSOLETE_TABLES:
            assert _table_exists(migrated_url, table), table
        assert _table_exists(migrated_url, "job_runs")
        command.upgrade(_alembic_config(), "head")
        for table in NEW_TABLES:
            assert _table_exists(migrated_url, table), table
    finally:
        _restore_migration_env(saved)


def test_clean_reset_removes_agent_history_and_spares_generic(
    migrated_url: str,
) -> None:
    saved = _set_migration_env(migrated_url)
    try:
        command.downgrade(_alembic_config(), "0013")
        _seed_old_world(migrated_url)
        # The reset executes as part of the REAL 0014 upgrade.
        command.upgrade(_alembic_config(), "head")
    finally:
        _restore_migration_env(saved)
        # Leave the fixture at head regardless of failure order (a later
        # test must never inherit a downgraded schema).
        saved_again = _set_migration_env(migrated_url)
        try:
            command.upgrade(_alembic_config(), "head")
        finally:
            _restore_migration_env(saved_again)

    with psycopg.connect(migrated_url, autocommit=True) as conn:
        agent_runs = conn.execute(
            """
            SELECT COUNT(*) FROM job_runs
            WHERE run_id IN (%s, %s)
            """,
            (AGENT_ROOT, AGENT_CHILD),
        ).fetchone()
        assert agent_runs is not None and agent_runs[0] == 0
        # Dependent agent rows gone with their runs.
        for table, column in (
            ("job_steps", "run_id"),
            ("job_events", "run_id"),
            ("job_tags", "job_id"),
            ("job_schedule_fires", "run_id"),
        ):
            orphan = conn.execute(
                f"""
                SELECT COUNT(*) FROM {table}
                WHERE {column} IN (%s, %s)
                """,
                (AGENT_ROOT, AGENT_CHILD),
            ).fetchone()
            assert orphan is not None and orphan[0] == 0, table
        # Objective bindings of agent-owned runs are gone (FK requirement).
        bound = conn.execute(
            """
            SELECT COUNT(*) FROM objective_runs
            WHERE run_id IN (%s, %s)
            """,
            (AGENT_ROOT, AGENT_CHILD),
        ).fetchone()
        assert bound is not None and bound[0] == 0

        # Unrelated generic rows survive with their exact counts.
        assert conn.execute(
            "SELECT COUNT(*) FROM job_runs WHERE run_id = %s", (UNRELATED_RUN,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM job_steps WHERE run_id = %s", (UNRELATED_RUN,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM job_events WHERE run_id = %s", (UNRELATED_RUN,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM job_tags WHERE job_id = %s", (UNRELATED_RUN,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM job_schedule_fires WHERE run_id = %s",
            (UNRELATED_RUN,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM job_schedules WHERE schedule_id = %s",
            (UNRELATED_SCHEDULE,),
        ).fetchone()[0] == 1

        # The tag catalog survives: reserved + terminal seeds.
        tags = {
            row[0]
            for row in conn.execute("SELECT name FROM tags").fetchall()
        }
        assert {"unclassified", "succeeded", "failed", "cancelled"} <= tags

        # The obsolete chat/task/objective tables were NOT dropped (D09).
        for table in OBSOLETE_TABLES:
            assert _table_exists(migrated_url, table), table

        # The retained generic surface still executes after the reset.
        conn.execute("SELECT COUNT(*) FROM job_runs")
        conn.execute(
            """
            SELECT t.name FROM job_tags jt JOIN tags t ON t.tag_id = jt.tag_id
            WHERE jt.job_id = %s
            """,
            (UNRELATED_RUN,),
        ).fetchone()


def test_destructive_ddl_drops_obsolete_and_spares_generic(
    migrated_url: str,
) -> None:
    """Prove the authored DDL on the real database (NOT in the chain)."""
    import importlib.util
    import pathlib
    import sqlalchemy

    module_path = (
        pathlib.Path(BACKEND_ROOT)
        / "migrations"
        / "versions"
        / "0014_agent_sessions.py"
    )
    spec = importlib.util.spec_from_file_location("mai_migration_0014", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destructive = module.run_destructive_ddl
    # The obsolete tables exist at head (they are dropped at D09 only); a
    # failed earlier test must never inherit a downgraded schema.
    saved = _set_migration_env(migrated_url)
    try:
        command.upgrade(_alembic_config(), "head")
    finally:
        _restore_migration_env(saved)
    engine = sqlalchemy.create_engine(migrated_url)
    try:
        with engine.begin() as conn:
            destructive(conn)
    finally:
        engine.dispose()

    with psycopg.connect(migrated_url, autocommit=True) as conn:
        for table in OBSOLETE_TABLES:
            assert not _table_exists(migrated_url, table), table
        for table in (
            "job_runs",
            "job_steps",
            "job_events",
            "job_tags",
            "tags",
            "job_schedules",
            "job_schedule_fires",
            "sessions",
            "session_entries",
        ):
            assert _table_exists(migrated_url, table), table
        # The generic surface is still queryable and populated.
        assert conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] >= 4
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM job_runs WHERE run_id = %s",
                (UNRELATED_RUN,),
            ).fetchone()[0]
            == 1
        )
        # No dangling FK: the retained rows still join their catalog.
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM job_tags jt
                JOIN tags t ON t.tag_id = jt.tag_id
                """
            ).fetchone()[0]
            >= 1
        )
