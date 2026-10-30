"""Focused migration tests for the mandatory history-tag schema (W1, revision 0011).

Claims under test (plans/history-tags.md slice 1 — schema only):

1. The fresh 0010 → 0011 upgrade seeds the reserved ``unclassified`` catalog
   row exactly once and backfills every existing job run and conversation
   with it (``assignment_source='system'``).
2. Catalog bounds/normalization: display name 1..64 single-line printable
   chars stored exactly whitespace-normalized (trimmed, collapsed), optional
   description <= 500 chars; canonical uniqueness is case- and
   whitespace-insensitive (``ux_tags_name_canonical`` on ``lower(name)``).
3. Both association tables carry ``(entity_id, tag_id)`` primary keys and the
   reverse ``(tag_id, entity_id)`` indexes.
4. Mandatory at-least-one-tag invariant against out-of-band SQL: a new entity
   plus its first association commit within one transaction (deferred
   constraint triggers), a committed entity can never have zero tags, deleting
   the last association cannot commit, multi-tag removal keeping one commits,
   and deleting the entity cascades only its associations.
5. Deletion policy at schema level: an unused tag deletes, a used tag is
   refused (RESTRICT — the store/API "force" policy of a later slice works by
   removing associations first); the reserved ``unclassified`` row can never
   be deleted.
6. The 0011 downgrade removes the schema (tables, triggers, functions) without
   touching entity rows, and the re-upgrade rebuilds seed + backfill + the
   live invariant.

Linear-chain/single-head and idempotent re-upgrade assertions live in the
migration-head owner (``tests/test_job_schema.py``) per the established
chain-tip convention; this file only pins 0011's own revision constants.

Every storage claim runs against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real linear Alembic chain applied — the
established ``test_job_schema`` harness shape.
"""

from __future__ import annotations

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

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Frozen identities for the reserved catalog row — mirrored from the 0011
# migration module (loaded by path below), asserted equal so a drift fails
# loudly.
RESERVED_ID = "6a1e9d3c-5b4f-4c2a-9e8d-7f0a1b2c3d4e"

SERVER_ID = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")

JOB_A = uuid.UUID("a1e2c3d4-0001-4001-8001-0000000000a1")
JOB_B = uuid.UUID("a1e2c3d4-0002-4002-8002-0000000000a2")
JOB_C = uuid.UUID("a1e2c3d4-0003-4003-8003-0000000000a3")
JOB_D = uuid.UUID("a1e2c3d4-0004-4004-8004-0000000000a4")
JOB_E = uuid.UUID("a1e2c3d4-0005-4005-8005-0000000000a5")
JOB_F = uuid.UUID("a1e2c3d4-0006-4006-8006-0000000000a6")
JOB_G = uuid.UUID("a1e2c3d4-0007-4007-8007-0000000000a7")
CONV_A = uuid.UUID("b2f3d4e5-0001-4001-8001-0000000000b1")
CONV_B = uuid.UUID("b2f3d4e5-0002-4002-8002-0000000000b2")
CONV_C = uuid.UUID("b2f3d4e5-0003-4003-8003-0000000000b3")
CONV_D = uuid.UUID("b2f3d4e5-0004-4004-8004-0000000000b4")
TAG_KEEPER = uuid.UUID("c3a4b5d6-0001-4001-8001-0000000000c1")
TAG_USED = uuid.UUID("c3a4b5d6-0002-4002-8002-0000000000c2")
TAG_TEMP = uuid.UUID("c3a4b5d6-0003-4003-8003-0000000000c3")
TAG_X = uuid.UUID("c3a4b5d6-0004-4004-8004-0000000000c4")
TAG_Y = uuid.UUID("c3a4b5d6-0005-4005-8005-0000000000c5")
TAG_Z = uuid.UUID("c3a4b5d6-0006-4006-8006-0000000000c6")
TAG_M1 = uuid.UUID("c3a4b5d6-0007-4007-8007-0000000000c7")
TAG_M2 = uuid.UUID("c3a4b5d6-0008-4008-8008-0000000000c8")
TAG_U1 = uuid.UUID("c3a4b5d6-0009-4009-8009-0000000000c9")

_MIGRATION_PATH = BACKEND_ROOT / "migrations" / "versions" / "0011_history_tags.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0011", _MIGRATION_PATH)
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
        # Whole-schema reset (the chat/agent suites' approach): a version row
        # without its tables would make the upgrade a silent no-op.
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
    """Fresh database carried to ``target`` (pre-0011 for backfill proofs)."""
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
        with psycopg.connect(_database_url(base_url, "postgres"), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _insert_bare_job_run(url: str, run_id: uuid.UUID) -> None:
    """Pre-0011 entity row without classification (only valid below 0011)."""
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
        conn.commit()


def _insert_bare_conversation(url: str, conversation_id: uuid.UUID) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO conversations (conversation_id, server_id) VALUES (%s, %s)",
            (conversation_id, SERVER_ID),
        )
        conn.commit()


def _insert_job_run(url: str, run_id: uuid.UUID) -> None:
    # The mandatory-tag invariant requires creation and first classification
    # in ONE transaction — exactly the discipline the later tag store uses
    # (assign ``unclassified`` at creation time).
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
            (run_id, uuid.UUID(RESERVED_ID)),
        )
        conn.commit()


def _insert_conversation(url: str, conversation_id: uuid.UUID) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO conversations (conversation_id, server_id) VALUES (%s, %s)",
            (conversation_id, SERVER_ID),
        )
        conn.execute(
            "INSERT INTO conversation_tags (conversation_id, tag_id) VALUES (%s, %s)",
            (conversation_id, uuid.UUID(RESERVED_ID)),
        )
        conn.commit()


def _insert_tag(
    url: str,
    name: str,
    tag_id: uuid.UUID | None = None,
    description: str | None = None,
) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO tags (tag_id, name, description) VALUES (%s, %s, %s)",
            (tag_id or uuid.uuid4(), name, description),
        )
        conn.commit()


def _assign_job_tag(url: str, job_id: uuid.UUID, tag_id: uuid.UUID) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO job_tags (job_id, tag_id) VALUES (%s, %s)",
            (job_id, tag_id),
        )
        conn.commit()


def _assign_conversation_tag(
    url: str, conversation_id: uuid.UUID, tag_id: uuid.UUID
) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO conversation_tags (conversation_id, tag_id) VALUES (%s, %s)",
            (conversation_id, tag_id),
        )
        conn.commit()


def _commit_raises(conn: psycopg.Connection, exc_type: type[Exception]):
    with pytest.raises(exc_type) as excinfo:
        conn.commit()
    conn.rollback()
    return excinfo.value


def _fetchall(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Revision identity
# ---------------------------------------------------------------------------


def test_revision_constants_target_the_0010_parent() -> None:
    module = _migration_module()
    assert module.revision == "0011"
    assert module.down_revision == "0010"
    assert module.RESERVED_UNCLASSIFIED_TAG_ID == RESERVED_ID


# ---------------------------------------------------------------------------
# 1 — fresh upgrade: reserved seed + backfill of existing entities
# ---------------------------------------------------------------------------


def test_fresh_upgrade_seeds_reserved_and_backfills_existing_entities(
    migrated_url: str,
) -> None:
    with _isolated_database(migrated_url, "mai_fxt_w1_tags_fresh", "0010") as url:
        # Entities exist BEFORE the 0011 upgrade: the backfill must classify
        # every one of them.
        _insert_bare_job_run(url, JOB_A)
        _insert_bare_job_run(url, JOB_B)
        _insert_bare_conversation(url, CONV_A)

        command.upgrade(_alembic_config(), "head")

        rows = _fetchall(url, "SELECT tag_id, name, description FROM tags")
        assert len(rows) == 1
        reserved = rows[0]
        assert reserved[0] == uuid.UUID(RESERVED_ID)
        assert reserved[1] == "unclassified"
        assert reserved[2] is not None and len(reserved[2]) <= 500

        job_rows = _fetchall(
            url,
            "SELECT job_id, tag_id, assignment_source, assigned_by, created_at "
            "FROM job_tags ORDER BY job_id",
        )
        assert [row[0] for row in job_rows] == [JOB_A, JOB_B]
        assert all(
            row[1] == uuid.UUID(RESERVED_ID)
            and row[2] == "system"
            and row[3] == ""
            and row[4] is not None
            for row in job_rows
        )

        conv_rows = _fetchall(
            url,
            "SELECT conversation_id, tag_id, assignment_source, assigned_by "
            "FROM conversation_tags",
        )
        assert len(conv_rows) == 1
        assert conv_rows[0][0] == CONV_A
        assert conv_rows[0][1] == uuid.UUID(RESERVED_ID)
        assert conv_rows[0][2] == "system" and conv_rows[0][3] == ""


# ---------------------------------------------------------------------------
# 2 — bounds + normalization + canonical uniqueness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "constraint"),
    [
        ("", "ck_tags_name_len"),
        ("x" * 65, "ck_tags_name_len"),
        (" leading", "ck_tags_name_normalized"),
        ("trailing ", "ck_tags_name_normalized"),
        ("a  b", "ck_tags_name_normalized"),
        ("a\tb", "ck_tags_name_normalized"),
        ("a\nb", "ck_tags_name_normalized"),
        ("a\x01b", "ck_tags_name_printable"),
    ],
)
def test_tag_name_bound_violations_name_the_failing_check(
    migrated_url: str, name: str, constraint: str
) -> None:
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
            conn.execute("INSERT INTO tags (tag_id, name) VALUES (%s, %s)", (uuid.uuid4(), name))
    assert excinfo.value.diag.constraint_name == constraint


def test_tag_description_and_unicode_bounds(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
            conn.execute(
                "INSERT INTO tags (tag_id, name, description) VALUES (%s, %s, %s)",
                (uuid.uuid4(), "bounded", "d" * 501),
            )
        assert excinfo.value.diag.constraint_name == "ck_tags_description_len"
        conn.rollback()
        # Boundary + non-ASCII display names are legal.
        conn.execute(
            "INSERT INTO tags (tag_id, name, description) VALUES (%s, %s, %s)",
            (TAG_TEMP, "тег 001", "d" * 500),
        )
        conn.commit()
    assert (
        _fetchall(migrated_url, "SELECT name FROM tags WHERE tag_id = %s", (TAG_TEMP,))[0][0]
        == "тег 001"
    )


def test_canonical_uniqueness_is_case_insensitive(migrated_url: str) -> None:
    _insert_tag(migrated_url, "Deploy", TAG_X)
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation) as excinfo:
            conn.execute(
                "INSERT INTO tags (tag_id, name) VALUES (%s, %s)",
                (uuid.uuid4(), "deploy"),
            )
    assert excinfo.value.diag.constraint_name == "ux_tags_name_canonical"

    _insert_tag(migrated_url, "Foo Bar", TAG_Y)
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO tags (tag_id, name) VALUES (%s, %s)",
                (uuid.uuid4(), "foo bar"),
            )


# ---------------------------------------------------------------------------
# 3 — keys and reverse indexes
# ---------------------------------------------------------------------------


def test_primary_keys_and_reverse_indexes(migrated_url: str) -> None:
    defs = dict(
        _fetchall(
            migrated_url,
            "SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY(%s)",
            (["ix_job_tags_tag_id", "ix_conversation_tags_tag_id", "ux_tags_name_canonical"],),
        )
    )
    assert set(defs) == {
        "ix_job_tags_tag_id",
        "ix_conversation_tags_tag_id",
        "ux_tags_name_canonical",
    }
    assert "(tag_id, job_id)" in defs["ix_job_tags_tag_id"]
    assert "(tag_id, conversation_id)" in defs["ix_conversation_tags_tag_id"]
    assert "UNIQUE" in defs["ux_tags_name_canonical"]
    assert "lower(name)" in defs["ux_tags_name_canonical"]

    pkeys = dict(
        _fetchall(
            migrated_url,
            """
            SELECT c.relname, pg_get_indexdef(i.indexrelid)
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            WHERE i.indisprimary AND c.relname IN
                ('job_tags_pkey', 'conversation_tags_pkey')
            """,
        )
    )
    assert "(job_id, tag_id)" in pkeys["job_tags_pkey"]
    assert "(conversation_id, tag_id)" in pkeys["conversation_tags_pkey"]


# ---------------------------------------------------------------------------
# 4 — mandatory at-least-one-tag invariant (deferred constraint triggers)
# ---------------------------------------------------------------------------


def test_new_entity_plus_association_commits_in_one_transaction(
    migrated_url: str,
) -> None:
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            """
            INSERT INTO job_runs
                (run_id, server_id, depth, profile_revision, prompt_revision,
                 model_profile, requested_grants)
            VALUES (%s, %s, 0, 1, 1, 'profile', '[]'::jsonb)
            """,
            (JOB_C, SERVER_ID),
        )
        conn.execute(
            "INSERT INTO job_tags (job_id, tag_id) VALUES (%s, %s)",
            (JOB_C, uuid.UUID(RESERVED_ID)),
        )
        conn.execute(
            "INSERT INTO conversations (conversation_id, server_id) VALUES (%s, %s)",
            (CONV_B, SERVER_ID),
        )
        conn.execute(
            "INSERT INTO conversation_tags (conversation_id, tag_id) VALUES (%s, %s)",
            (CONV_B, uuid.UUID(RESERVED_ID)),
        )
        conn.commit()
    assert _fetchall(
        migrated_url, "SELECT count(*) FROM job_tags WHERE job_id = %s", (JOB_C,)
    ) == [(1,)]
    assert (
        _fetchall(
            migrated_url,
            "SELECT count(*) FROM conversation_tags WHERE conversation_id = %s",
            (CONV_B,),
        )
        == [(1,)]
    )


def test_committed_entity_without_any_tag_cannot_commit(migrated_url: str) -> None:
    for insert_sql, params, exc_type in (
        (
            """
            INSERT INTO job_runs
                (run_id, server_id, depth, profile_revision, prompt_revision,
                 model_profile, requested_grants)
            VALUES (%s, %s, 0, 1, 1, 'profile', '[]'::jsonb)
            """,
            (JOB_D, SERVER_ID),
            psycopg.errors.RaiseException,
        ),
        (
            "INSERT INTO conversations (conversation_id, server_id) VALUES (%s, %s)",
            (CONV_C, SERVER_ID),
            psycopg.errors.RaiseException,
        ),
    ):
        with psycopg.connect(migrated_url) as conn:
            conn.execute(insert_sql, params)
            error = _commit_raises(conn, exc_type)
        assert "must keep at least one tag" in str(error)


def test_deleting_the_last_association_cannot_commit(migrated_url: str) -> None:
    _insert_job_run(migrated_url, JOB_E)
    with psycopg.connect(migrated_url) as conn:
        conn.execute("DELETE FROM job_tags WHERE job_id = %s", (JOB_E,))
        error = _commit_raises(conn, psycopg.errors.RaiseException)
    assert "must keep at least one tag" in str(error)
    # The failed commit left the association in place.
    assert _fetchall(
        migrated_url, "SELECT count(*) FROM job_tags WHERE job_id = %s", (JOB_E,)
    ) == [(1,)]


def test_multi_tag_removal_is_valid_while_one_remains(migrated_url: str) -> None:
    _insert_tag(migrated_url, "multi-keeper", TAG_M1)
    _insert_tag(migrated_url, "multi-used", TAG_M2)
    _insert_job_run(migrated_url, JOB_B)
    for tag in (TAG_M1, TAG_M2):
        _assign_job_tag(migrated_url, JOB_B, tag)
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "DELETE FROM job_tags WHERE job_id = %s AND tag_id IN (%s, %s)",
            (JOB_B, uuid.UUID(RESERVED_ID), TAG_M1),
        )
        conn.commit()
    remaining = _fetchall(
        migrated_url, "SELECT tag_id FROM job_tags WHERE job_id = %s", (JOB_B,)
    )
    assert remaining == [(TAG_M2,)]
    with psycopg.connect(migrated_url) as conn:
        conn.execute("DELETE FROM job_tags WHERE job_id = %s", (JOB_B,))
        error = _commit_raises(conn, psycopg.errors.RaiseException)
    assert "must keep at least one tag" in str(error)


def test_entity_deletion_cascades_associations_only(migrated_url: str) -> None:
    _insert_job_run(migrated_url, JOB_A)
    _insert_tag(migrated_url, "ops-cascade", TAG_Z)
    _assign_job_tag(migrated_url, JOB_A, TAG_Z)
    _insert_conversation(migrated_url, CONV_A)
    _assign_conversation_tag(migrated_url, CONV_A, TAG_Z)

    with psycopg.connect(migrated_url) as conn:
        conn.execute("DELETE FROM job_runs WHERE run_id = %s", (JOB_A,))
        conn.execute("DELETE FROM conversations WHERE conversation_id = %s", (CONV_A,))
        conn.commit()

    assert _fetchall(
        migrated_url, "SELECT count(*) FROM job_tags WHERE job_id = %s", (JOB_A,)
    ) == [(0,)]
    assert (
        _fetchall(
            migrated_url,
            "SELECT count(*) FROM conversation_tags WHERE conversation_id = %s",
            (CONV_A,),
        )
        == [(0,)]
    )
    # The catalog rows survive the association-only cascade.
    assert _fetchall(
        migrated_url, "SELECT count(*) FROM tags WHERE tag_id = %s", (TAG_Z,)
    ) == [(1,)]


# ---------------------------------------------------------------------------
# 5 — tag deletion policy at schema level; reserved row protection
# ---------------------------------------------------------------------------


def test_unused_tag_deletes_and_used_tag_is_refused(migrated_url: str) -> None:
    _insert_tag(migrated_url, "temp-unused", TAG_U1)
    with psycopg.connect(migrated_url) as conn:
        conn.execute("DELETE FROM tags WHERE tag_id = %s", (TAG_U1,))
        conn.commit()

    _insert_tag(migrated_url, "used-tag", TAG_USED)
    _insert_job_run(migrated_url, JOB_F)
    _assign_job_tag(migrated_url, JOB_F, TAG_USED)
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.IntegrityError) as excinfo:
            conn.execute("DELETE FROM tags WHERE tag_id = %s", (TAG_USED,))
        conn.rollback()
    # ON DELETE RESTRICT reports 23001 (restrict_violation) on some server
    # versions and 23503 (foreign_key_violation) on others; either way the
    # used tag's deletion is refused at the integrity layer.
    assert excinfo.value.sqlstate in {"23001", "23503"}
    # The later store/API "force" path works through the same schema: remove
    # the association first (the entity keeps the reserved tag), then delete.
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "DELETE FROM job_tags WHERE job_id = %s AND tag_id = %s",
            (JOB_F, TAG_USED),
        )
        conn.commit()
    with psycopg.connect(migrated_url) as conn:
        conn.execute("DELETE FROM tags WHERE tag_id = %s", (TAG_USED,))
        conn.commit()


def test_reserved_unclassified_cannot_be_deleted(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            conn.execute("DELETE FROM tags WHERE tag_id = %s", (uuid.UUID(RESERVED_ID),))
        conn.rollback()
    assert "reserved unclassified tag cannot be deleted" in str(excinfo.value)

    # Not even with zero associations left: the row itself is undeletable
    # while the schema exists (the guard is the row identity, not its FK).
    _insert_tag(migrated_url, "replacement-class", TAG_KEEPER)
    _insert_job_run(migrated_url, JOB_G)
    _assign_job_tag(migrated_url, JOB_G, TAG_KEEPER)
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "DELETE FROM job_tags WHERE job_id = %s AND tag_id = %s",
            (JOB_G, uuid.UUID(RESERVED_ID)),
        )
        conn.commit()
    with psycopg.connect(migrated_url) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM tags WHERE tag_id = %s", (uuid.UUID(RESERVED_ID),))
        conn.rollback()
    assert _fetchall(migrated_url, "SELECT count(*) FROM tags WHERE tag_id = %s", (uuid.UUID(RESERVED_ID),)) == [(1,)]


# ---------------------------------------------------------------------------
# 6 — downgrade removes the whole schema; re-upgrade rebuilds the invariant
# ---------------------------------------------------------------------------


def test_downgrade_then_reupgrade_rebuilds_seed_backfill_and_invariant(
    migrated_url: str,
) -> None:
    with _isolated_database(migrated_url, "mai_fxt_w1_tags_down", "head") as url:
        _insert_job_run(url, JOB_A)
        _insert_conversation(url, CONV_A)
        _insert_tag(url, "keeper", TAG_KEEPER)

        command.downgrade(_alembic_config(), "0010")

        with psycopg.connect(url, autocommit=True) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            leftovers = conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name "
                "IN ('tags', 'job_tags', 'conversation_tags')"
            ).fetchone()
            functions = conn.execute(
                "SELECT count(*) FROM pg_proc WHERE proname LIKE 'mai_history_tags%'"
            ).fetchone()
            triggers = conn.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_%' "
                "AND NOT tgisinternal"
            ).fetchone()
        assert version == ("0010",)
        assert leftovers == functions == triggers == (0,)
        # Entity rows survive the schema removal untouched.
        assert _fetchall(url, "SELECT count(*) FROM job_runs") == [(1,)]
        assert _fetchall(url, "SELECT count(*) FROM conversations") == [(1,)]

        command.upgrade(_alembic_config(), "head")

        # Reserved row is re-seeded; the 'keeper' catalog row is gone with the
        # dropped table; both entities are re-backfilled.
        assert _fetchall(url, "SELECT tag_id, name FROM tags") == [
            (uuid.UUID(RESERVED_ID), "unclassified")
        ]
        assert _fetchall(url, "SELECT count(*) FROM job_tags WHERE job_id = %s", (JOB_A,)) == [(1,)]
        assert (
            _fetchall(
                url,
                "SELECT count(*) FROM conversation_tags WHERE conversation_id = %s",
                (CONV_A,),
            )
            == [(1,)]
        )

        # The rebuilt invariant is live: a bare entity cannot commit again.
        with psycopg.connect(url) as conn:
            conn.execute(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision, prompt_revision,
                     model_profile, requested_grants)
                VALUES (%s, %s, 0, 1, 1, 'profile', '[]'::jsonb)
                """,
                (JOB_B, SERVER_ID),
            )
            error = _commit_raises(conn, psycopg.errors.RaiseException)
        assert "must keep at least one tag" in str(error)
