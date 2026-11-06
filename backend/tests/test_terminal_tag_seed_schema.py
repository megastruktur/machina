"""Focused migration tests for the terminal-tag seed (W1 fix, revision 0012).

Claims under test (fresh-install classification bug: a production database
migrated fresh to 0011 held only ``unclassified``, so every terminal
transition raised ``TagStoreError``/``TAG_NOT_FOUND`` from
``_aclassify_terminal_job_locked``):

1. The fresh upgrade through head creates exactly the three terminal rows
   (``succeeded`` / ``failed`` / ``cancelled``) with their stable explicit
   ids and simple descriptions, alongside the reserved ``unclassified`` row.
2. Idempotency: re-running the upgrade never duplicates or overwrites the
   seeded rows; a user tag created under the same name (any casing) BEFORE
   the upgrade is preserved verbatim — its id and description survive and
   no second row with the seed id appears.
3. The downgrade removes only seeded ids that carry no association; used
   seeded tags, user tags and every association row survive untouched.

Every claim runs against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real linear Alembic chain — the
established ``test_history_tag_schema`` harness shape.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.engine import make_url

from mai.database import create_app_engine
from mai.tag_store import aclassify_terminal_job

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

RESERVED_ID = uuid.UUID("6a1e9d3c-5b4f-4c2a-9e8d-7f0a1b2c3d4e")
SEED_SUCCEEDED_ID = uuid.UUID("7707533f-b618-4f6a-86a4-f3fb22f79ad0")
SEED_FAILED_ID = uuid.UUID("24119527-3bd3-4224-938b-ac407805c886")
SEED_CANCELLED_ID = uuid.UUID("c0d58dcb-378b-4c2c-8d78-4ba45f57b3d0")
SEED_IDS = (SEED_SUCCEEDED_ID, SEED_FAILED_ID, SEED_CANCELLED_ID)
SEED_NAMES = ("succeeded", "failed", "cancelled")

SERVER_ID = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
JOB_A = uuid.UUID("d4e5f6a7-0001-4001-8001-0000000000d1")
USER_SUCCEEDED_ID = uuid.UUID("d4e5f6a7-0002-4002-8002-0000000000d2")

_MIGRATION_PATH = BACKEND_ROOT / "migrations" / "versions" / "0012_terminal_tag_seed.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0012", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(ALEMBIC_INI))


def _database_url(url: str, database: str) -> str:
    # ``hide_password=False``: the default masks the password as ``***``,
    # which then fails SCRAM on every derived-URL connect.
    return make_url(url).set(database=database).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database upgraded from zero to the current head."""
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the storage claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        # Whole-schema reset: a version row without its tables would make
        # the upgrade a silent no-op.
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@contextmanager
def _isolated_database(base_url: str, name: str, target: str):
    """Fresh database carried to ``target`` (0011 = pre-seed state)."""
    db_url = _database_url(base_url, name)
    with psycopg.connect(_database_url(base_url, "postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = db_url
    try:
        command.upgrade(_alembic_config(), target)
        yield db_url
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
        with psycopg.connect(
            _database_url(base_url, "postgres"), autocommit=True
        ) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _insert_user_tag(url: str, tag_id: uuid.UUID, name: str, description: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO tags (tag_id, name, description) VALUES (%s, %s, %s)",
            (tag_id, name, description),
        )
        conn.commit()


def _insert_classified_job_run(url: str, run_id: uuid.UUID, tag_id: uuid.UUID) -> None:
    # The mandatory-tag invariant requires creation and first classification
    # in ONE transaction (0011's deferred constraint triggers).
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO job_runs
                (run_id, server_id, depth, profile_revision, prompt_revision,
                 model_profile, requested_grants)
            VALUES (%s, %s, 0, 1, 1, 'profile', '[]'::jsonb)
            """,
            (run_id, SERVER_ID),
        )
        conn.execute(
            "INSERT INTO job_tags (job_id, tag_id) VALUES (%s, %s)",
            (run_id, tag_id),
        )
        conn.commit()


def _fetchall(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Revision identity
# ---------------------------------------------------------------------------


def test_revision_constants_target_the_0011_parent() -> None:
    module = _migration_module()
    assert module.revision == "0012"
    assert module.down_revision == "0011"
    assert module.TERMINAL_SUCCEEDED_TAG_ID == str(SEED_SUCCEEDED_ID)
    assert module.TERMINAL_FAILED_TAG_ID == str(SEED_FAILED_ID)
    assert module.TERMINAL_CANCELLED_TAG_ID == str(SEED_CANCELLED_ID)


# ---------------------------------------------------------------------------
# 1 — fresh upgrade through head seeds the three terminal rows
# ---------------------------------------------------------------------------


def test_fresh_upgrade_through_head_seeds_terminal_tags(migrated_url: str) -> None:
    with _isolated_database(migrated_url, "mai_fxt_w1_seed_fresh", "0011") as url:
        before = _fetchall(url, "SELECT tag_id, name FROM tags ORDER BY tag_id")
        assert before == [(RESERVED_ID, "unclassified")]

        command.upgrade(_alembic_config(), "head")

        rows = _fetchall(url, "SELECT tag_id, name, description FROM tags")
        assert len(rows) == 4  # reserved + exactly the three terminal seeds
        assert {(row[0], row[1]) for row in rows} == {
            (RESERVED_ID, "unclassified"),
            (SEED_SUCCEEDED_ID, "succeeded"),
            (SEED_FAILED_ID, "failed"),
            (SEED_CANCELLED_ID, "cancelled"),
        }
        assert all(row[2] is not None and 0 < len(row[2]) <= 500 for row in rows)


# ---------------------------------------------------------------------------
# 2 — idempotency: no duplication, no overwrite, same-name user tags kept
# ---------------------------------------------------------------------------


def test_upgrade_preserves_same_name_user_tags_and_is_idempotent(
    migrated_url: str,
) -> None:
    with _isolated_database(migrated_url, "mai_fxt_w1_seed_user", "0011") as url:
        # A user already classified with a custom "Succeeded" tag (different
        # id, own description) BEFORE the upgrade.
        _insert_user_tag(
            url, USER_SUCCEEDED_ID, "Succeeded", "team's own success semantics"
        )

        command.upgrade(_alembic_config(), "head")

        # The user row survived verbatim; no seed-id duplicate appeared.
        succeeded_rows = _fetchall(
            url,
            "SELECT tag_id, name, description FROM tags WHERE lower(name) = 'succeeded'",
        )
        assert succeeded_rows == [
            (USER_SUCCEEDED_ID, "Succeeded", "team's own success semantics")
        ]
        assert _fetchall(
            url, "SELECT count(*) FROM tags WHERE tag_id = %s", (SEED_SUCCEEDED_ID,)
        ) == [(0,)]

        # The other two names were seeded normally.
        seeded = _fetchall(
            url,
            "SELECT tag_id, name FROM tags WHERE lower(name) = ANY(%(names)s)",
            {"names": list(SEED_NAMES[1:])},
        )
        assert {(tag_id, name) for tag_id, name in seeded} == {
            (SEED_FAILED_ID, "failed"),
            (SEED_CANCELLED_ID, "cancelled"),
        }

        # A second upgrade at the tip is a no-op: nothing duplicated or
        # overwritten, the version stays at the chain head (0015 since D09).
        command.upgrade(_alembic_config(), "head")
        with psycopg.connect(url) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version == ("0015",)
        assert _fetchall(url, "SELECT count(*) FROM tags")[0][0] == 4
        assert _fetchall(
            url,
            "SELECT count(*) FROM tags WHERE tag_id = ANY(%(ids)s)",
            {"ids": [str(tag_id) for tag_id in (*SEED_IDS, RESERVED_ID)]},
        ) == [(3,)]


# ---------------------------------------------------------------------------
# 3 — downgrade removes only unused seeded ids; user data never touched
# ---------------------------------------------------------------------------


def test_downgrade_removes_only_unused_seeded_ids(migrated_url: str) -> None:
    # D09: revision 0015's honest no-op downgrade makes the chain below 0014
    # unreachable from head, so this proof of 0012's own downgrade behavior
    # builds a fresh database at 0012 (the seed revision) and downgrades one
    # step only — the same revision pair the test always exercised.
    with _isolated_database(migrated_url, "mai_fxt_w1_seed_down", "0012") as url:
        # One run classified with the seeded succeeded tag: its id is USED.
        _insert_classified_job_run(url, JOB_A, SEED_SUCCEEDED_ID)

        command.downgrade(_alembic_config(), "0011")

        # The used seeded id survived with its association; unused seeded
        # ids were removed; user data untouched.
        assert _fetchall(
            url, "SELECT count(*) FROM tags WHERE tag_id = %s", (SEED_SUCCEEDED_ID,)
        ) == [(1,)]
        assert _fetchall(
            url,
            "SELECT count(*) FROM job_tags WHERE job_id = %s AND tag_id = %s",
            (JOB_A, SEED_SUCCEEDED_ID),
        ) == [(1,)]
        assert _fetchall(
            url,
            "SELECT count(*) FROM tags WHERE tag_id = ANY(%(ids)s)",
            {"ids": [str(tag_id) for tag_id in (SEED_FAILED_ID, SEED_CANCELLED_ID)]},
        ) == [(0,)]
        assert _fetchall(
            url, "SELECT count(*) FROM tags WHERE tag_id = %s", (RESERVED_ID,)
        ) == [(1,)]

        # Re-upgrade re-seeds only the removed rows; the adopted id is not
        # duplicated (the isolated database stays at the 0012 seed revision).
        command.upgrade(_alembic_config(), "0012")
        assert _fetchall(
            url,
            "SELECT count(*) FROM tags WHERE lower(name) = ANY(%(names)s)",
            {"names": list(SEED_NAMES)},
        ) == [(3,)]
        assert _fetchall(
            url, "SELECT count(*) FROM job_tags WHERE job_id = %s", (JOB_A,)
        ) == [(1,)]


# ---------------------------------------------------------------------------
# 4 — the fixed production symptom: terminal classification finds its tag
# ---------------------------------------------------------------------------


def test_terminal_classification_succeeds_on_fresh_install(
    migrated_url: str,
) -> None:
    """The reported failure: every terminal transition ended in
    ``TagStoreError``/``TAG_NOT_FOUND`` on a fresh 0011 database because
    only ``unclassified`` existed. With the seed present the lifecycle
    writer classifies instead. Sync on purpose: alembic's env.py drives
    its async engine with asyncio.run(), which must not nest inside a
    test event loop."""
    with _isolated_database(migrated_url, "mai_fxt_w1_seed_classify", "0011") as url:
        command.upgrade(_alembic_config(), "head")

        _insert_classified_job_run(url, JOB_A, RESERVED_ID)
        _, sessions = create_app_engine(url)

        async def _classify():
            return await aclassify_terminal_job(
                sessions, JOB_A, server_id=SERVER_ID, terminal_tag_name="succeeded"
            )

        outcome = asyncio.run(_classify())
        assert outcome.classified is True
        assert outcome.tag_id == SEED_SUCCEEDED_ID
        assert outcome.reason == "classified"

        rows = _fetchall(
            url,
            "SELECT tag_id, assignment_source FROM job_tags WHERE job_id = %s",
            (JOB_A,),
        )
        assert rows == [(SEED_SUCCEEDED_ID, "system")]
