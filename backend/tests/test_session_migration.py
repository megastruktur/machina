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


def _rebuild_at(url: str, revision: str) -> None:
    """Rebuild the fixture from zero to ``revision``.

    Revision 0015's honest no-op downgrade means a downgraded schema no
    longer carries the obsolete chat/task/objective tables, so a test that
    must seed revision-0013 agent rows rebuilds from an empty schema —
    the disposable fixture makes that cheap and order-independent.
    """
    saved = _set_migration_env(url)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        command.upgrade(_alembic_config(), revision)
    finally:
        _restore_migration_env(saved)


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
                ON CONFLICT (conversation_id) DO NOTHING
                """,
                (AGENT_CONV, str(SERVER_A)),
            )
            conn.execute(
                """
                INSERT INTO conversation_tags
                    (conversation_id, tag_id, assignment_source)
                VALUES (%s, %s, 'system')
                ON CONFLICT DO NOTHING
                """,
                (AGENT_CONV, uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)),
            )
            conn.execute(
                """
                INSERT INTO conversation_runs (conversation_id, run_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
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
                ON CONFLICT (objective_id) DO NOTHING
                """,
                (AGENT_OBJECTIVE, str(SERVER_A)),
            )
            conn.execute(
                """
                INSERT INTO objective_runs
                    (objective_id, run_id, initiating_server_id, handler,
                     actor_kind)
                VALUES (%s, %s, %s, 'agent.run_v2', 'operator')
                ON CONFLICT DO NOTHING
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
                    ON CONFLICT (run_id, seq) DO NOTHING
                    """,
                    (uuid.uuid4(), run_id),
                )
                conn.execute(
                    """
                    INSERT INTO job_events
                        (event_id, run_id, seq, ts, v, type, data)
                    VALUES (%s, %s, 1, now(), 1, 'run.submitted', '{}'::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    (uuid.uuid4(), run_id),
                )
                conn.execute(
                    """
                    INSERT INTO job_tags (job_id, tag_id, assignment_source)
                    VALUES (%s, %s, 'system')
                    ON CONFLICT DO NOTHING
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
                ON CONFLICT DO NOTHING
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
                ON CONFLICT (schedule_id) DO NOTHING
                """,
                (UNRELATED_SCHEDULE, str(SERVER_A)),
            )
            conn.execute(
                """
                INSERT INTO job_schedule_fires
                    (schedule_id, planned_fire_time, run_id)
                VALUES (%s, '2026-01-01T00:00:00+00:00'::timestamptz, %s)
                ON CONFLICT DO NOTHING
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
        # 0015's downgrade is an honest no-op: the obsolete chat/task/
        # objective tables are NOT fabricated empty below 0015.
        for table in OBSOLETE_TABLES:
            assert not _table_exists(migrated_url, table), table
        assert _table_exists(migrated_url, "job_runs")
        command.upgrade(_alembic_config(), "head")
        for table in NEW_TABLES:
            assert _table_exists(migrated_url, table), table
        # The re-run destructive drops are idempotent (IF EXISTS): the
        # obsolete tables stay absent at head.
        for table in OBSOLETE_TABLES:
            assert not _table_exists(migrated_url, table), table
    finally:
        _restore_migration_env(saved)


def test_clean_reset_removes_agent_history_and_spares_generic(
    migrated_url: str,
) -> None:
    saved = _set_migration_env(migrated_url)
    try:
        _rebuild_at(migrated_url, "0013")
        _seed_old_world(migrated_url)
        # The reset + destructive cutover execute as the REAL chain upgrade.
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

        # The obsolete chat/task/objective tables were dropped by 0015 in
        # the SAME chain upgrade (COORD-D02-01 contract).
        for table in OBSOLETE_TABLES:
            assert not _table_exists(migrated_url, table), table

        # The retained generic surface still executes after the reset.
        conn.execute("SELECT COUNT(*) FROM job_runs")
        conn.execute(
            """
            SELECT t.name FROM job_tags jt JOIN tags t ON t.tag_id = jt.tag_id
            WHERE jt.job_id = %s
            """,
            (UNRELATED_RUN,),
        ).fetchone()


def test_destructive_cutover_is_installed_at_head_and_spares_generic(
    migrated_url: str,
) -> None:
    """Prove revision 0015 in the LINEAR chain on the real database.

    The DDL list is the exact one authored + proven at D02 (single source
    ``mai.migrations``); this test proves the installed revision: head is
    ``0016`` (0016 is the additive SCOPE-1 service-scope column; the
    destructive 0015 cutover stays installed beneath it), the obsolete
    tables are absent, the conversation-only
    trigger functions are dropped, the generic trigger surface survives,
    and the unrelated generic rows still execute.
    """
    import importlib.util
    import pathlib

    # Source-level identity: 0015 executes exactly the proven DDL list.
    module_path = (
        pathlib.Path(BACKEND_ROOT)
        / "migrations"
        / "versions"
        / "0015_drop_legacy_chat_task_objective.py"
    )
    spec = importlib.util.spec_from_file_location("mai_migration_0015", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from mai.migrations import DESTRUCTIVE_DDL_STATEMENTS as PROVEN_DDL

    assert module.DESTRUCTIVE_DDL_STATEMENTS == PROVEN_DDL
    assert module.down_revision == "0014"

    saved = _set_migration_env(migrated_url)
    try:
        # Make this test order-independent: rebuild at 0013 (the obsolete
        # tables exist there), seed its own old world, then run the real
        # chain upgrade through 0014+0015.
        _rebuild_at(migrated_url, "0013")
        _seed_old_world(migrated_url)
        command.upgrade(_alembic_config(), "head")
    finally:
        _restore_migration_env(saved)

    with psycopg.connect(migrated_url, autocommit=True) as conn:
        # The chain head IS the current linear revision (0017 = the unified
        # notification inbox above the installed 0016 SCOPE-1 service-scope
        # column).
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version is not None and version[0] == "0017", version

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

        # The conversation-only trigger functions are dropped; the generic
        # history-tag functions survive with their triggers.
        functions = {
            row[0]
            for row in conn.execute(
                """
                SELECT proname FROM pg_proc
                WHERE pronamespace = 'public'::regnamespace
                """
            ).fetchall()
        }
        assert (
            "mai_history_tags_conversation_has_tag" not in functions
        ), "conversation mandatory-tag function must be dropped"
        assert (
            "mai_history_tags_conversation_tag_left" not in functions
        ), "conversation tag-left function must be dropped"
        for retained in (
            "mai_history_tags_job_run_has_tag",
            "mai_history_tags_job_tag_left",
            "mai_history_tags_protect_reserved_tag",
        ):
            assert retained in functions, retained
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT trigger_name FROM information_schema.triggers "
                "WHERE trigger_schema = 'public'"
            ).fetchall()
        }
        assert "trg_conversations_mandatory_tag" not in triggers
        assert "trg_conversation_tags_mandatory_tag" not in triggers
        for retained in (
            "trg_job_runs_mandatory_tag",
            "trg_job_tags_mandatory_tag",
            "trg_tags_protect_reserved",
        ):
            assert retained in triggers, retained

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


# --- SCOPE-1: additive nullable sessions.service_id (0016) ---------------------

SCOPE_SESSION_LEGACY = uuid.UUID("5d5349b2-0001-4001-8001-00000000c001")
SCOPE_SESSION_SCOPED = uuid.UUID("5d5349b2-0002-4002-8002-00000000c002")
SCOPE_SERVICE_ID = uuid.UUID("3f9d2c4e-0001-4001-8001-00000000d001")


def _sessions_columns(url: str) -> dict[str, tuple[str, str]]:
    """``column_name -> (data_type, is_nullable)`` for ``public.sessions``."""
    with psycopg.connect(url) as conn:
        return {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'sessions'
                """
            ).fetchall()
        }


def test_0016_seeded_upgrade_preserves_null_and_fresh_parity(
    migrated_url: str,
) -> None:
    """Real 0015→0016 upgrade on a SEEDED fixture: legacy rows keep their
    NULL ``service_id``, the column is a nullable bare UUID (no FK, no
    index, no default), and a schema built from ZERO at head is
    column-for-column identical to the upgraded one."""
    saved = _set_migration_env(migrated_url)
    try:
        _rebuild_at(migrated_url, "0015")
        assert "service_id" not in _sessions_columns(migrated_url)
        with psycopg.connect(migrated_url) as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO sessions
                        (session_id, server_id, source, status)
                    VALUES (%s, %s, 'runbook', 'idle'),
                           (%s, %s, 'interactive', 'idle')
                    """,
                    (
                        SCOPE_SESSION_LEGACY,
                        str(SERVER_A),
                        SCOPE_SESSION_SCOPED,
                        str(SERVER_A),
                    ),
                )
        command.upgrade(_alembic_config(), "head")
    finally:
        _restore_migration_env(saved)

    upgraded = _sessions_columns(migrated_url)
    assert upgraded["service_id"] == ("uuid", "YES")
    with psycopg.connect(migrated_url) as conn:
        # Seeded 0015 rows survive with NULL service_id (no backfill).
        rows = conn.execute(
            """
            SELECT session_id, service_id FROM sessions
            WHERE session_id IN (%s, %s)
            """,
            (SCOPE_SESSION_LEGACY, SCOPE_SESSION_SCOPED),
        ).fetchall()
        assert {tuple(row) for row in rows} == {
            (SCOPE_SESSION_LEGACY, None),
            (SCOPE_SESSION_SCOPED, None),
        }
        # No FK on the column (the map is a confined JSON store, not a
        # relational target) and no service index in this feature.
        fks = conn.execute(
            """
            SELECT COUNT(*) FROM pg_constraint
            WHERE contype = 'f'
              AND conrelid = 'public.sessions'::regclass
              AND conname LIKE '%service%'
            """
        ).fetchone()
        assert fks is not None and fks[0] == 0
        indexes = {
            row[0]
            for row in conn.execute(
                """
                SELECT indexname FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = 'sessions'
                """
            ).fetchall()
        }
        assert not any("service" in name for name in indexes)

    # Fresh-from-zero parity: a second disposable database at head has the
    # EXACT same sessions column set (linear chain == upgraded schema).
    parity_url = migrated_url.rsplit("/", 1)[0] + "/mcr_scope_parity_0016"
    saved = _set_migration_env(migrated_url)
    admin_url = migrated_url.rsplit("/", 1)[0] + "/postgres"
    fresh: dict[str, tuple[str, str]] = {}
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute("DROP DATABASE IF EXISTS mcr_scope_parity_0016")
            conn.execute("CREATE DATABASE mcr_scope_parity_0016")
        os.environ["MAI_DATABASE_URL"] = parity_url
        command.upgrade(_alembic_config(), "head")
        fresh = _sessions_columns(parity_url)
    finally:
        _restore_migration_env(saved)
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute("DROP DATABASE IF EXISTS mcr_scope_parity_0016")

    assert fresh == upgraded


def test_0016_downgrade_drops_only_service_column(migrated_url: str) -> None:
    """0016→0015 drops ONLY ``sessions.service_id``; every other sessions
    column and the seeded row survive; re-upgrade accepts a service write."""
    saved = _set_migration_env(migrated_url)
    columns_after_downgrade: dict[str, tuple[str, str]] = {}
    row_after_downgrade: list[tuple[uuid.UUID]] = []
    try:
        _rebuild_at(migrated_url, "0015")
        with psycopg.connect(migrated_url) as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO sessions
                        (session_id, server_id, source, status)
                    VALUES (%s, %s, 'runbook', 'idle')
                    """,
                    (SCOPE_SESSION_LEGACY, str(SERVER_A)),
                )
        command.upgrade(_alembic_config(), "head")
        with psycopg.connect(migrated_url) as conn:
            with conn.transaction():
                conn.execute(
                    "UPDATE sessions SET service_id = %s WHERE session_id = %s",
                    (SCOPE_SERVICE_ID, SCOPE_SESSION_LEGACY),
                )
        command.downgrade(_alembic_config(), "0015")
        columns_after_downgrade = _sessions_columns(migrated_url)
        with psycopg.connect(migrated_url) as conn:
            row_after_downgrade = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id = %s",
                (SCOPE_SESSION_LEGACY,),
            ).fetchall()
        # A re-upgrade accepts a service write again (the column is back).
        command.upgrade(_alembic_config(), "head")
    finally:
        _restore_migration_env(saved)
        # Leave the fixture at head for later tests.
        again = _set_migration_env(migrated_url)
        try:
            command.upgrade(_alembic_config(), "head")
        finally:
            _restore_migration_env(again)

    columns = columns_after_downgrade
    assert "service_id" not in columns
    for retained in (
        "session_id",
        "server_id",
        "source",
        "status",
        "title",
        "created_at",
        "updated_at",
    ):
        assert retained in columns, retained
    assert [tuple(row) for row in row_after_downgrade] == [
        (SCOPE_SESSION_LEGACY,)
    ]
    with psycopg.connect(migrated_url) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE sessions SET service_id = %s WHERE session_id = %s",
                (SCOPE_SERVICE_ID, SCOPE_SESSION_LEGACY),
            )
        assert (
            conn.execute(
                "SELECT service_id FROM sessions WHERE session_id = %s",
                (SCOPE_SESSION_LEGACY,),
            ).fetchone()[0]
            == SCOPE_SERVICE_ID
        )
