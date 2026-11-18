"""Focused tests for the service-knowledge migration (0018).

Acceptance (ask_10 §AC1-AC3; campaign contracts C4/D3/D4/D5; bounds and
vocabularies are the frozen constants of
``mai.applications.knowledge_contracts``):

1. ``upgrade`` from zero to head succeeds; the FOUR new tables exist
   with their named CHECK constraints, named (unique) indexes and the
   frozen vocabularies/bounds — the live CHECK definitions are extracted
   from the catalog and compared against the contract enums/constants,
   so a drift fails loudly (0007 discipline).
2. Upgrade from the previous head (0017) with EXISTING data is safe: a
   representative ``application_projection`` row (+ journal row) and a
   ``notification_alerts`` row seeded at 0017 survive byte-for-byte, the
   four new tables start empty, and the canonical map/manifest surface
   is untouched (C4: no projection rewrite).
3. Constraint enforcement on the new tables: statement/key/source_ref
   bounds, frozen status/source-kind/operation-status vocabularies,
   revision/generation >= 1, unique ACTIVE semantic key per
   identity/generation (superseded/retracted rows may coexist; unrelated
   keys merge), unique fact revisions (FK to the fact), operation dedup
   on ``(source_run_id, source_tool_call_id)`` and ``workflow_id``, the
   64-hex payload digest, and the evidenced map-only manual identity
   WITHOUT an ``application_projection`` row (no FK to the adoption
   projection, C4).
4. ``downgrade`` one revision (0018 → 0017) drops ONLY the four new
   tables (every pre-existing table/row survives) and a re-upgrade works.
5. No secret surface: no new column name suggests a secret/transcript/
   blob (live ``information_schema.columns`` extraction).

Boundary discipline: every claim runs against the real disposable
PostgreSQL fixture (``MAI_TEST_DATABASE_URL``; the chain needs the
``vector`` extension) with the real linear Alembic chain. Fixtures carry
only fixed opaque UUIDs and synthetic text.
"""

from __future__ import annotations

import json
import os
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from mai.applications.knowledge_contracts import (
    MAX_FACT_KEY_CHARS,
    MAX_FACT_STATEMENT_CHARS,
    MAX_SOURCE_REF_CHARS,
    FactSourceKind,
    FactStatus,
    OperationStatus,
    SubEffectStatus,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")
APP_A = uuid.UUID("a1b2c3d4-0001-4001-8001-0000000000a1")

NEW_TABLES = (
    "service_knowledge_identity",
    "service_learned_fact",
    "service_learned_fact_revision",
    "service_learning_operation",
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
    """Rebuild the fixture from zero to ``revision`` (the disposable
    fixture makes that cheap and order-independent)."""
    saved = _set_migration_env(url)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        command.upgrade(_alembic_config(), revision)
    finally:
        _restore_migration_env(saved)


def _upgrade_to(url: str, revision: str) -> None:
    saved = _set_migration_env(url)
    try:
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
            "disposable Postgres fixture"
        )
    _rebuild_at(url, "head")
    return url


def _table_exists(url: str, table: str) -> bool:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
        ).fetchone()
    return bool(row and row[0])


def _indexes(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            ).fetchall()
        }


def _check_definitions(url: str, table: str) -> dict[str, str]:
    """Live CHECK constraint definitions for one table, by name."""
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            """
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE contype = 'c' AND conrelid = %s::regclass
            """,
            (f"public.{table}",),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def _check_literals(definition: str) -> set[str]:
    """The quoted string literals inside one CHECK definition (post-
    normalization keeps them as ``'value'`` / ``'value'::character
    varying`` fragments)."""
    import re

    return set(re.findall(r"'([a-z_]+)'", definition))


def _columns(url: str, table: str) -> list[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        ).fetchall()
    return [row[0] for row in rows]


def _insert_fact(
    url: str,
    *,
    fact_id: uuid.UUID,
    server_id: uuid.UUID = SERVER_A,
    application_id: uuid.UUID = APP_A,
    generation: int = 1,
    category: str = "deployment",
    key: str = "health-path",
    statement: str = "health lives at /healthz",
    revision: int = 1,
    status: str = "active",
    source_kind: str = "tool_result",
    source_ref: str = "run:1:step:2",
) -> None:
    """Insert one learned fact in its own transaction (committed on
    success, rolled back and closed on a violation)."""
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO service_learned_fact (
                fact_id, server_id, application_id, generation, category,
                key, statement, revision, status, source_kind, source_ref,
                source_run_id, verified_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                fact_id,
                server_id,
                application_id,
                generation,
                category,
                key,
                statement,
                revision,
                status,
                source_kind,
                source_ref,
                uuid.uuid4(),
            ),
        )


# ---------------------------------------------------------------------------
# 1. upgrade: tables, indexes, named checks, frozen vocabularies/bounds
# ---------------------------------------------------------------------------


def test_upgrade_creates_tables_indexes_and_checks(migrated_url: str) -> None:
    for table in NEW_TABLES:
        assert _table_exists(migrated_url, table), table
    indexes = _indexes(migrated_url)
    for expected in (
        "uq_service_learned_fact_active_key",
        "ix_service_learned_fact_scope",
        "ix_service_learned_fact_history",
        "ix_service_learned_fact_revision_history",
        "uq_service_learning_operation_source",
        "uq_service_learning_operation_workflow",
        "ix_service_learning_operation_status",
    ):
        assert expected in indexes, expected

    expected_checks = {
        "service_knowledge_identity": (
            "ck_service_knowledge_identity_generation",
            "ck_service_knowledge_identity_service_revision",
            "ck_service_knowledge_identity_knowledge_revision",
            "ck_service_knowledge_identity_suppressed_generation",
        ),
        "service_learned_fact": (
            "ck_service_learned_fact_generation",
            "ck_service_learned_fact_category",
            "ck_service_learned_fact_key",
            "ck_service_learned_fact_statement",
            "ck_service_learned_fact_revision",
            "ck_service_learned_fact_status",
            "ck_service_learned_fact_source_kind",
            "ck_service_learned_fact_source_ref",
            "ck_service_learned_fact_source_tool_call_id",
            "ck_service_learned_fact_conflict_reason",
        ),
        "service_learned_fact_revision": (
            "ck_service_learned_fact_revision_revision",
            "ck_service_learned_fact_revision_statement",
            "ck_service_learned_fact_revision_status",
            "ck_service_learned_fact_revision_source_kind",
            "ck_service_learned_fact_revision_source_ref",
        ),
        "service_learning_operation": (
            "ck_service_learning_operation_generation",
            "ck_service_learning_operation_payload_digest",
            "ck_service_learning_operation_workflow_id",
            "ck_service_learning_operation_status",
            "ck_service_learning_operation_sub_effects_vocabulary",
            "ck_service_learning_operation_coalesce_key",
            "ck_service_learning_operation_source_tool_call_id",
            "ck_service_learning_operation_actor",
            "ck_service_learning_operation_error_detail",
        ),
    }
    for table, names in expected_checks.items():
        checks = _check_definitions(migrated_url, table)
        for name in names:
            assert name in checks, (table, name)


def test_live_vocabulary_matches_contract_enums(migrated_url: str) -> None:
    """0007 discipline: extract the LIVE CHECK definitions and compare
    their quoted literals against the contract enums. PostgreSQL
    normalizes text-vocabulary CHECKs into
    ``ANY((ARRAY['x'::character varying, …])::text[])`` and JSONB
    containment checks into ``"value"`` fragments, so both shapes are
    asserted."""
    fact_checks = _check_definitions(migrated_url, "service_learned_fact")
    status_def = fact_checks["ck_service_learned_fact_status"]
    assert {value.value for value in FactStatus} <= _check_literals(status_def)
    source_def = fact_checks["ck_service_learned_fact_source_kind"]
    assert {value.value for value in FactSourceKind} <= _check_literals(source_def)
    operation_checks = _check_definitions(
        migrated_url, "service_learning_operation"
    )
    operation_def = operation_checks["ck_service_learning_operation_status"]
    assert {value.value for value in OperationStatus} <= _check_literals(
        operation_def
    )
    sub_effects_def = operation_checks[
        "ck_service_learning_operation_sub_effects_vocabulary"
    ]
    assert "jsonb_typeof" in sub_effects_def
    assert "jsonb_path_query_array" in sub_effects_def
    for value in SubEffectStatus:
        assert f'"{value.value}"' in sub_effects_def, value.value


def test_live_bounds_match_contract_constants(migrated_url: str) -> None:
    """Drift guard: the LIVE character bounds equal the frozen contract
    constants (statement 800, key 80, source_ref 256, category 64).
    PostgreSQL normalizes BETWEEN into (>= 1 AND <= N) comparisons."""
    fact_checks = _check_definitions(migrated_url, "service_learned_fact")
    statement_def = fact_checks["ck_service_learned_fact_statement"]
    assert f"char_length(statement) <= {MAX_FACT_STATEMENT_CHARS}" in statement_def
    assert "char_length(statement) >= 1" in statement_def
    key_def = fact_checks["ck_service_learned_fact_key"]
    assert f"char_length(key) <= {MAX_FACT_KEY_CHARS}" in key_def
    source_ref_def = fact_checks["ck_service_learned_fact_source_ref"]
    assert f"char_length(source_ref) <= {MAX_SOURCE_REF_CHARS}" in source_ref_def
    category_def = fact_checks["ck_service_learned_fact_category"]
    assert "char_length(category) <= 64" in category_def
    # The per-service ACTIVE fact capacity (64) is enforced by the store
    # layer, not the schema: the constant is pinned here so a silent
    # change fails this test loudly.
    from mai.applications.knowledge_contracts import (
        MAX_ACTIVE_FACTS_PER_SERVICE,
    )

    assert MAX_ACTIVE_FACTS_PER_SERVICE == 64


# ---------------------------------------------------------------------------
# 2. constraint enforcement
# ---------------------------------------------------------------------------


def test_fact_bounds_are_enforced(migrated_url: str) -> None:
    columns = (
        "INSERT INTO service_learned_fact (fact_id, server_id, "
        "application_id, generation, category, key, statement, revision, "
        "status, source_kind, source_ref, source_run_id, verified_at)"
    )
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                columns
                + " VALUES (%s, %s, %s, 1, 'c', 'k', %s, 1, 'active', "
                "'tool_result', %s, %s, now())",
                (
                    uuid.uuid4(),
                    str(SERVER_A),
                    str(APP_A),
                    "x" * 801,
                    "r" * 10,
                    uuid.uuid4(),
                ),
            )
        conn.rollback()
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                columns
                + " VALUES (%s, %s, %s, 1, 'c', %s, 'ok', 1, 'active', "
                "'tool_result', 'r', %s, now())",
                (
                    uuid.uuid4(),
                    str(SERVER_A),
                    str(APP_A),
                    "k" * 81,
                    uuid.uuid4(),
                ),
            )
        conn.rollback()


def test_fact_status_and_source_kind_vocabulary_enforced(
    migrated_url: str,
) -> None:
    fact_id = uuid.uuid4()
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO service_learned_fact (fact_id, server_id, "
                "application_id, generation, category, key, statement, "
                "revision, status, source_kind, source_ref, source_run_id, "
                "verified_at) VALUES (%s, %s, %s, 1, 'c', 'k', 's', 1, "
                "'vanished', 'tool_result', 'r', %s, now())",
                (fact_id, str(SERVER_A), str(APP_A), uuid.uuid4()),
            )
        conn.rollback()
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO service_learned_fact (fact_id, server_id, "
                "application_id, generation, category, key, statement, "
                "revision, status, source_kind, source_ref, source_run_id, "
                "verified_at) VALUES (%s, %s, %s, 1, 'c', 'k', 's', 1, "
                "'active', 'shouting', 'r', %s, now())",
                (fact_id, str(SERVER_A), str(APP_A), uuid.uuid4()),
            )
        conn.rollback()


def test_fact_source_ref_revision_generation_bounds_enforced(
    migrated_url: str,
) -> None:
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO service_learned_fact (fact_id, server_id, "
                "application_id, generation, category, key, statement, "
                "revision, status, source_kind, source_ref, source_run_id, "
                "verified_at) VALUES (%s, %s, %s, 1, 'c', 'k', 's', 1, "
                "'active', 'tool_result', %s, %s, now())",
                (uuid.uuid4(), str(SERVER_A), str(APP_A), "r" * 257, uuid.uuid4()),
            )
        conn.rollback()
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO service_learned_fact (fact_id, server_id, "
                "application_id, generation, category, key, statement, "
                "revision, status, source_kind, source_ref, source_run_id, "
                "verified_at) VALUES (%s, %s, %s, 1, 'c', 'k', 's', 0, "
                "'active', 'tool_result', 'r', %s, now())",
                (uuid.uuid4(), str(SERVER_A), str(APP_A), uuid.uuid4()),
            )
        conn.rollback()
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO service_learned_fact (fact_id, server_id, "
                "application_id, generation, category, key, statement, "
                "revision, status, source_kind, source_ref, source_run_id, "
                "verified_at) VALUES (%s, %s, %s, 0, 'c', 'k', 's', 1, "
                "'active', 'tool_result', 'r', %s, now())",
                (uuid.uuid4(), str(SERVER_A), str(APP_A), uuid.uuid4()),
            )
        conn.rollback()


def test_active_semantic_key_unique_per_identity(migrated_url: str) -> None:
    _insert_fact(
        migrated_url,
        fact_id=uuid.UUID(int=0xA01),
        key="health-path",
        server_id=SERVER_A,
        application_id=APP_A,
        generation=1,
        category="deployment",
    )
    # Same ACTIVE tuple again: rejected (unique partial index).
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO service_learned_fact (fact_id, server_id, "
                "application_id, generation, category, key, statement, "
                "revision, status, source_kind, source_ref, source_run_id, "
                "verified_at) VALUES (%s, %s, %s, 1, 'deployment', "
                "'health-path', 's', 1, 'active', 'tool_result', 'r', "
                "%s, now())",
                (uuid.uuid4(), str(SERVER_A), str(APP_A), uuid.uuid4()),
            )
        conn.rollback()
    # Non-active statuses may coexist on the same semantic key.
    _insert_fact(
        migrated_url,
        fact_id=uuid.UUID(int=0xA02),
        status="superseded",
        key="health-path",
        server_id=SERVER_A,
        application_id=APP_A,
        generation=1,
        category="deployment",
    )
    _insert_fact(
        migrated_url,
        fact_id=uuid.UUID(int=0xA03),
        status="retracted",
        key="health-path",
        server_id=SERVER_A,
        application_id=APP_A,
        generation=1,
        category="deployment",
    )
    # A different key merges (unrelated keys coexist).
    _insert_fact(
        migrated_url,
        fact_id=uuid.UUID(int=0xA04),
        key="compose-directory",
        server_id=SERVER_A,
        application_id=APP_A,
        generation=1,
        category="deployment",
    )
    # A different generation is a distinct semantic space.
    _insert_fact(
        migrated_url,
        fact_id=uuid.UUID(int=0xA05),
        key="health-path",
        server_id=SERVER_A,
        application_id=APP_A,
        generation=2,
        category="deployment",
    )


def test_revision_history_constraints(migrated_url: str) -> None:
    fact_id = uuid.UUID(int=0xB01)
    _insert_fact(
        migrated_url,
        fact_id=fact_id,
        key="history-isolation",
        server_id=SERVER_A,
        application_id=APP_A,
        generation=1,
        category="deployment",
    )
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO service_learned_fact_revision (revision_id, "
            "fact_id, server_id, application_id, revision, statement, "
            "status, source_kind, source_ref, changed_at) VALUES (%s, %s, "
            "%s, %s, 1, 'first statement', 'active', 'tool_result', 'r', "
            "now())",
            (uuid.uuid4(), fact_id, str(SERVER_A), str(APP_A)),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO service_learned_fact_revision (revision_id, "
                "fact_id, server_id, application_id, revision, statement, "
                "status, source_kind, source_ref, changed_at) VALUES (%s, "
                "%s, %s, %s, 1, 'duplicate revision', 'active', "
                "'tool_result', 'r', now())",
                (uuid.uuid4(), fact_id, str(SERVER_A), str(APP_A)),
            )
        conn.rollback()
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO service_learned_fact_revision (revision_id, "
                "fact_id, server_id, application_id, revision, statement, "
                "status, source_kind, source_ref, changed_at) VALUES (%s, "
                "%s, %s, %s, 1, 'orphan', 'active', 'tool_result', 'r', "
                "now())",
                (uuid.uuid4(), uuid.uuid4(), str(SERVER_A), str(APP_A)),
            )
        conn.rollback()


def _insert_operation(
    url: str,
    *,
    operation_id: uuid.UUID,
    run_id: uuid.UUID,
    tool_call_id: str,
    workflow_id: str,
    payload_digest: str = "a" * 64,
    status: str = "accepted",
) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO service_learning_operation (operation_id, "
            "server_id, application_id, payload_digest, workflow_id, "
            "status, sub_effects, coalesce_key, source_run_id, "
            "source_tool_call_id, actor) VALUES (%s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s)",
            (
                operation_id,
                str(SERVER_A),
                str(APP_A),
                payload_digest,
                workflow_id,
                status,
                json.dumps({"facts": "pending"}),
                f"coalesce:{tool_call_id}",
                run_id,
                tool_call_id,
                "runtime",
            ),
        )


def test_operation_dedup_digest_and_status_enforced(migrated_url: str) -> None:
    import json

    run_id = uuid.uuid4()
    _insert_operation(
        migrated_url,
        operation_id=uuid.UUID(int=0xC01),
        run_id=run_id,
        tool_call_id="call-1",
        workflow_id="wf-unique-1",
    )
    # Same (source_run_id, source_tool_call_id): rejected (dedup arbiter).
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO service_learning_operation (operation_id, "
                "server_id, application_id, payload_digest, workflow_id, "
                "status, sub_effects, coalesce_key, source_run_id, "
                "source_tool_call_id, actor) VALUES (%s, %s, %s, %s, %s, "
                "'queued', '{}', 'k2', %s, 'call-1', 'runtime')",
                (
                    uuid.uuid4(),
                    str(SERVER_A),
                    str(APP_A),
                    "b" * 64,
                    "wf-other-1",
                    run_id,
                ),
            )
        conn.rollback()
    # Same workflow_id: rejected (deterministic workflow identity).
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO service_learning_operation (operation_id, "
                "server_id, application_id, payload_digest, workflow_id, "
                "status, sub_effects, coalesce_key, source_run_id, "
                "source_tool_call_id, actor) VALUES (%s, %s, %s, %s, %s, "
                "'queued', '{}', 'k3', %s, 'call-2', 'runtime')",
                (
                    uuid.uuid4(),
                    str(SERVER_A),
                    str(APP_A),
                    "c" * 64,
                    "wf-unique-1",
                    uuid.uuid4(),
                ),
            )
        conn.rollback()
    # Non-64-lowercase-hex digest: rejected.
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO service_learning_operation (operation_id, "
                "server_id, application_id, payload_digest, workflow_id, "
                "status, sub_effects, coalesce_key, source_run_id, "
                "source_tool_call_id, actor) VALUES (%s, %s, %s, %s, %s, "
                "'accepted', '{}', 'k4', %s, 'call-3', 'runtime')",
                (
                    uuid.uuid4(),
                    str(SERVER_A),
                    str(APP_A),
                    "Z" * 64,
                    "wf-unique-2",
                    uuid.uuid4(),
                ),
            )
        conn.rollback()
    # Status outside the frozen vocabulary: rejected.
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO service_learning_operation (operation_id, "
                "server_id, application_id, payload_digest, workflow_id, "
                "status, sub_effects, coalesce_key, source_run_id, "
                "source_tool_call_id, actor) VALUES (%s, %s, %s, %s, %s, "
                "'done', '{}', 'k5', %s, 'call-4', 'runtime')",
                (
                    uuid.uuid4(),
                    str(SERVER_A),
                    str(APP_A),
                    "d" * 64,
                    "wf-unique-3",
                    uuid.uuid4(),
                ),
            )
        conn.rollback()


def test_identity_constraints_and_map_only_manual_identity(
    migrated_url: str,
) -> None:
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO service_knowledge_identity (server_id, "
                "application_id, generation) VALUES (%s, %s, 0)",
                (str(SERVER_A), str(APP_A)),
            )
        conn.rollback()
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO service_knowledge_identity (server_id, "
            "application_id, generation, manual_identity) VALUES (%s, %s, "
            "1, true)",
            (str(SERVER_A), str(APP_A)),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO service_knowledge_identity (server_id, "
                "application_id, generation) VALUES (%s, %s, 1)",
                (str(SERVER_A), str(APP_A)),
            )
        conn.rollback()
    # The application_id has NO row in application_projection: ACCEPTED
    # (the evidenced map-only manual identity — no FK to the adoption
    # projection, C4).
    with psycopg.connect(migrated_url) as conn:
        projection_count = conn.execute(
            "SELECT count(*) FROM application_projection WHERE "
            "application_id = %s",
            (str(APP_A),),
        ).fetchone()[0]
    assert projection_count == 0


def test_operation_sub_effects_vocabulary_enforced(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO service_learning_operation (operation_id, "
                "server_id, application_id, payload_digest, workflow_id, "
                "status, sub_effects, coalesce_key, source_run_id, "
                "source_tool_call_id, actor) VALUES (%s, %s, %s, %s, %s, "
                "'applying', %s, 'k6', %s, 'call-9', 'runtime')",
                (
                    uuid.uuid4(),
                    str(SERVER_A),
                    str(APP_A),
                    "e" * 64,
                    "wf-unique-4",
                    json.dumps({"facts": "half-done"}),
                    uuid.uuid4(),
                ),
            )
        conn.rollback()


# ---------------------------------------------------------------------------
# 3. 0017 data survives the upgrade; canonical surface untouched
# ---------------------------------------------------------------------------


def _seed_0017_rows(url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Representative 0017-era rows: an adoption projection and a
    notification alert (fresh identities per call)."""
    projection_id = uuid.uuid4()
    alert_id = uuid.uuid4()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO application_projection (application_id, server_id, "
            "name, origin, compose_project, working_dir, revision, "
            "schema_revision, sha256) VALUES (%s, %s, 'seeded-svc', "
            "'compose', 'seeded', '/srv/seeded', 1, 1, %s)",
            (projection_id, str(SERVER_A), "c" * 64),
        )
        conn.execute(
            "INSERT INTO notification_alerts (alert_id, server_id, origin, "
            "dedup_key, title, delivery_status, retain_until) VALUES (%s, "
            "%s, 'finding', %s, 'seeded title', 'pending', now())",
            (alert_id, str(SERVER_A), f"seeded-dedup-{alert_id}"),
        )
    return projection_id, alert_id


def test_0017_data_survives_upgrade_to_0018(migrated_url: str) -> None:
    _rebuild_at(migrated_url, "0017")
    projection_id, alert_id = _seed_0017_rows(migrated_url)
    _upgrade_to(migrated_url, "head")

    for table in NEW_TABLES:
        assert _table_exists(migrated_url, table), table
    with psycopg.connect(migrated_url) as conn:
        counts = {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in NEW_TABLES
        }
    assert counts == {name: 0 for name in NEW_TABLES}

    # The pre-existing rows are STILL PRESENT, byte-for-byte.
    with psycopg.connect(migrated_url) as conn:
        projection = conn.execute(
            "SELECT name, origin, compose_project, working_dir, revision, "
            "schema_revision, sha256 FROM application_projection WHERE "
            "application_id = %s",
            (projection_id,),
        ).fetchone()
        alert = conn.execute(
            "SELECT dedup_key, title, delivery_status FROM "
            "notification_alerts WHERE alert_id = %s",
            (alert_id,),
        ).fetchone()
    assert projection == (
        "seeded-svc",
        "compose",
        "seeded",
        "/srv/seeded",
        1,
        1,
        "c" * 64,
    )
    assert alert[0] == f"seeded-dedup-{alert_id}"
    assert alert[1] == "seeded title"
    assert alert[2] == "pending"

    # The canonical projection keeps EXACTLY its authored columns (no
    # knowledge columns leaked onto the map surface, C4).
    projection_columns = _columns(migrated_url, "application_projection")
    for leaked in ("statement", "generation", "source_kind", "verified_at"):
        assert leaked not in projection_columns


def test_no_secret_surface_columns(migrated_url: str) -> None:
    forbidden = {
        "value",
        "secret",
        "password",
        "token",
        "transcript",
        "output",
        "blob",
    }
    for table in NEW_TABLES:
        columns = _columns(migrated_url, table)
        assert columns, table
        leaked = forbidden.intersection(column.lower() for column in columns)
        assert leaked == set(), (table, leaked)


# ---------------------------------------------------------------------------
# 4. downgrade drops ONLY the four new tables; re-upgrade works
# ---------------------------------------------------------------------------


def test_downgrade_drops_only_new_tables(migrated_url: str) -> None:
    projection_id, alert_id = _seed_0017_rows(migrated_url)
    # Alembic `command.upgrade` only moves FORWARD: leaving 0018 requires
    # the explicit downgrade command.
    saved = _set_migration_env(migrated_url)
    try:
        command.downgrade(_alembic_config(), "0017")
    finally:
        _restore_migration_env(saved)
    for table in NEW_TABLES:
        assert not _table_exists(migrated_url, table), table
    # Pre-existing tables and rows are intact.
    with psycopg.connect(migrated_url) as conn:
        projection = conn.execute(
            "SELECT count(*) FROM application_projection WHERE "
            "application_id = %s",
            (projection_id,),
        ).fetchone()[0]
        alert = conn.execute(
            "SELECT count(*) FROM notification_alerts WHERE alert_id = %s",
            (alert_id,),
        ).fetchone()[0]
        sessions_table = _table_exists(migrated_url, "sessions")
        job_runs_table = _table_exists(migrated_url, "job_runs")
    assert projection == 1
    assert alert == 1
    assert sessions_table and job_runs_table

    # A re-upgrade works and recreates the four tables.
    _upgrade_to(migrated_url, "head")
    for table in NEW_TABLES:
        assert _table_exists(migrated_url, table), table
