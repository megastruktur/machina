"""Focused tests for the document metadata + vector storage schema (card 07-vector-schema).

Acceptance (BRIEF):

1. Selected embedding/model/dimensions and explicit server/shared scope are
   encoded in the schema: ``knowledge_doc_chunks`` carries the frozen
   dimension contract tuple (ADR §4.2: ``("openai-compatible",
   "text-embedding-3-small", 1536, "cosine")``) pinned by CHECK constraints
   alongside the ``vector(1536)`` column (HNSW ``vector_cosine_ops``), and
   ``knowledge_documents`` encodes the shared namespace as ``server_id IS
   NULL`` (NULLS NOT DISTINCT uniqueness, so the shared scope participates
   fully).
2. Source revision uniqueness (real constraint, real violations),
   tombstones (durable delete marker) and deletion relations (chunk cascade
   supporting the ADR §4.3 rebuild: delete → re-chunk → re-embed →
   re-index) — all in the controller's own database: no surprise second
   database, no engine-shadowing tables.

Boundary discipline (test policy, risk-critical slice): every storage claim
runs against a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``)
with the real linear Alembic chain applied — uniqueness/tombstone/CHECK
claims are proven by real integrity violations, the dimension claim by real
``vector_dims()`` / cosine-distance results, the FTS claim by real tsvector
matches, and the storage-boundary server filter by a real join query across
two servers plus the shared scope. Fixtures carry only TEST-NET-style fixed
UUIDs and dummy hex hashes — never secret values. The 05-job-schema chain
fixtures stay untouched: this card adds its own auxiliary experiment
databases (``mai_fxt_07_prior``, ``mai_fxt_07_down``).
"""

from __future__ import annotations

import os
import re
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Linear migration chain (exclusive migration-chain resource). Head
# assertions target the chain TIP; the tuple tracks the full chain.
# Chain-tip reconciliation (card 08-adoption, exclusive migration-chain
# owner): revision 0006 appends the application catalogue projection +
# journal; the tip constants are advanced here so this file's head
# assertions stay true.
REVISION_IDS = ("0001", "0002", "0003", "0004", "0005", "0006")

# The exact public application schema after 0006 — the 0005 set plus the
# two application catalogue tables (08-adoption); nothing else, in
# particular no second vector store, no engine-shadowing tables, no
# ``dbos`` schema before engine launch.
EXPECTED_APP_TABLES = frozenset(
    {
        "admin_credential",
        "admin_sessions",
        "alembic_version",
        "approvals",
        "profile_projection",
        "profile_projection_journal",
        "job_runs",
        "job_steps",
        "job_events",
        "job_schedules",
        "job_schedule_fires",
        "knowledge_documents",
        "knowledge_doc_chunks",
        "application_projection",
        "application_projection_journal",
    }
)

# Frozen dimension contract (knowledge-packaging ADR §4.2, contracts.md C07
# rule 3; mirrored from backend/tests/test_knowledge_dependencies.py — a
# drift between the two test pins fails loudly).
EMBEDDING_PROVIDER = "openai-compatible"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
DISTANCE_METRIC = "cosine"

DIMENSION_CONTRACT = (
    EMBEDDING_PROVIDER,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSIONS,
    DISTANCE_METRIC,
)

# Frozen fixed fixture identities (canonical lowercase UUID strings).
SERVER_A = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
SERVER_B = "8f1c2e3d-4a5b-4c6d-9e7f-0a1b2c3d4e5f"
APP_ID = "2e2f3a4b-5c6d-4e7f-8a9b-0c1d2e3f4a5b"
DOC_SHARED = "9d8c7b6a-5e4f-4a3b-2c1d-0e9f8a7b6c5d"
DOC_A = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
DOC_B = "3c4d5e6f-7a8b-4c9d-8e7f-0a1b2c3d4e5f"

SHA = "ab" * 32
OTHER_SHA = "cd" * 32

TS = "2026-10-16T01:02:03.123456+00:00"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _database_url(url: str, database: str) -> str:
    """Same server, different database (for isolated chain experiments)."""
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Full app set at the chain tip — the 08-adoption tables are listed
        # here (chain-tip reconciliation) so the zero-to-head fixture stays
        # order-independent after revision 0006.
        conn.execute(
            "DROP TABLE IF EXISTS application_projection_journal, "
            "application_projection, knowledge_doc_chunks, knowledge_documents, "
            "job_schedule_fires, job_schedules, "
            "job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version"
        )
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0006``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the schema claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
async def sessions(migrated_url: str):
    engine, factory = create_app_engine(migrated_url)
    yield factory
    # Each data test starts from an empty knowledge domain (session-scoped
    # fixture database): delete children before parents.
    async with factory() as session:
        await session.execute(text("DELETE FROM knowledge_doc_chunks"))
        await session.execute(text("DELETE FROM knowledge_documents"))
        await session.commit()
    await engine.dispose()


async def _exec(sessions, sql: str, **params: object) -> None:
    async with sessions() as session:
        await session.execute(text(sql), params)
        await session.commit()


async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _fetchall(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).all()


def _vec(values: list[float]) -> str:
    """pgvector text literal for a raw-SQL parameter."""
    return "[" + ",".join(repr(value) for value in values) + "]"


async def _insert_document(
    sessions,
    doc_id: str,
    *,
    revision: int = 1,
    server_id: str | None = None,
    app_id: str | None = None,
    namespace: str = "docs",
    sha256: str = SHA,
    tombstone: bool = False,
) -> str:
    """Insert one source-revision row; returns its document_key."""
    async with sessions() as session:
        key = (
            await session.execute(
                text(
                    """
                    INSERT INTO knowledge_documents
                        (doc_id, server_id, app_id, namespace, revision, sha256,
                         source_kind, origin, origin_revision, verification,
                         source_path, observed_at, tombstone, tombstoned_at)
                    VALUES
                        (:doc_id, :server_id, :app_id, :namespace, :revision,
                         :sha256, 'authored', 'operator', NULL, 'unverified',
                         :source_path, :observed_at, :tombstone, :tombstoned_at)
                    RETURNING document_key
                    """
                ),
                {
                    "doc_id": doc_id,
                    "server_id": server_id,
                    "app_id": app_id,
                    "namespace": namespace,
                    "revision": revision,
                    "sha256": sha256,
                    "source_path": f"servers/{server_id}/docs/{doc_id}.md"
                    if server_id
                    else f"shared/docs/{doc_id}.md",
                    "observed_at": TS,
                    "tombstone": tombstone,
                    "tombstoned_at": TS if tombstone else None,
                },
            )
        ).scalar()
        await session.commit()
    return str(key)


async def _insert_chunk(
    sessions,
    document_key: str,
    ord_: int,
    content: str,
    embedding: list[float],
    **overrides: object,
) -> None:
    columns = {
        "document_key": document_key,
        "ord": ord_,
        "content": content,
        "embedding": _vec(embedding),
        "embedding_provider": EMBEDDING_PROVIDER,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "distance_metric": DISTANCE_METRIC,
    }
    columns.update(overrides)
    await _exec(
        sessions,
        f"""
        INSERT INTO knowledge_doc_chunks
            ({", ".join(columns)})
        VALUES
            ({", ".join(f":{name}" for name in columns)})
        """,
        **columns,
    )


def _unit_vec(dims: int, first: float = 1.0) -> list[float]:
    vector = [0.0] * dims
    vector[0] = first
    return vector


def _flat_vec(value: float) -> list[float]:
    """Constant vector at the pinned dimension (irrelevant-distance filler)."""
    return [value] * EMBEDDING_DIMENSIONS


# ---------------------------------------------------------------------------
# Acceptance 1 — dimension contract + explicit server/shared scope in schema
# ---------------------------------------------------------------------------


class TestDimensionContract:
    def test_dimension_contract_tuple_is_the_frozen_v1_values(self) -> None:
        assert DIMENSION_CONTRACT == (
            "openai-compatible",
            "text-embedding-3-small",
            1536,
            "cosine",
        )

    def test_vector_bearing_table_carries_the_tuple_columns(
        self, migrated_url: str
    ) -> None:
        with psycopg.connect(migrated_url) as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'knowledge_doc_chunks'
                    """
                )
            }
        for column in (
            "embedding",
            "embedding_provider",
            "embedding_model",
            "embedding_dimensions",
            "distance_metric",
        ):
            assert column in columns, f"dimension contract column missing: {column}"

    def test_embedding_column_is_pinned_vector_type(self, migrated_url: str) -> None:
        with psycopg.connect(migrated_url) as conn:
            udt = conn.execute(
                """
                SELECT udt_name FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'knowledge_doc_chunks'
                  AND column_name = 'embedding'
                """
            ).fetchone()
            hnsw = conn.execute(
                """
                SELECT indexdef FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = 'knowledge_doc_chunks'
                  AND indexname = 'ix_knowledge_doc_chunks_embedding_hnsw'
                """
            ).fetchone()
        assert udt == ("vector",)
        assert hnsw is not None and "USING hnsw" in hnsw[0]
        assert "vector_cosine_ops" in hnsw[0]

    @pytest.mark.anyio
    async def test_vector_roundtrip_dims_and_cosine_distance(self, sessions) -> None:
        """Example vectors round-trip: pinned dims, finite cosine distance."""
        key = await _insert_document(sessions, DOC_SHARED)
        near = _unit_vec(EMBEDDING_DIMENSIONS, 1.0)
        far = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2) + [1.0]
        await _insert_chunk(sessions, key, 0, "alpha probe", near)
        await _insert_chunk(sessions, key, 1, "beta probe", far)
        row = await _fetchone(
            sessions,
            """
            SELECT vector_dims(embedding),
                   embedding <=> CAST(:query AS vector)
            FROM knowledge_doc_chunks
            WHERE document_key = :key AND ord = 0
            """,
            query=_vec(near),
            key=key,
        )
        assert row[0] == EMBEDDING_DIMENSIONS
        assert row[1] == pytest.approx(0.0)
        ordered = await _fetchall(
            sessions,
            """
            SELECT ord FROM knowledge_doc_chunks
            WHERE document_key = :key
            ORDER BY embedding <=> CAST(:query AS vector)
            """,
            query=_vec(near),
            key=key,
        )
        assert [r[0] for r in ordered] == [0, 1]

    @pytest.mark.anyio
    @pytest.mark.parametrize("bad_dims", [1535, 1537, 1, 3072])
    async def test_wrong_length_vector_rejected_by_column_type(
        self, sessions, bad_dims: int
    ) -> None:
        """ADR §4.2 first enforcement layer: the pinned column type."""
        key = await _insert_document(sessions, DOC_SHARED)
        with pytest.raises(DBAPIError):
            await _insert_chunk(
                sessions, key, 0, "dimension probe", _unit_vec(bad_dims)
            )

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "column",
        ["embedding_provider", "embedding_model",
         "embedding_dimensions", "distance_metric"],
    )
    async def test_dimension_tuple_pins_reject_other_models(
        self, sessions, column: str
    ) -> None:
        """ADR §4.2: one active (provider, model, dims) per index — a switch
        is a rebuild event, never a silently mixed row."""
        key = await _insert_document(sessions, DOC_SHARED)
        overrides = {
            "embedding_provider": "other-provider",
            "embedding_model": "other-model",
            "embedding_dimensions": 768,
            "distance_metric": "l2",
        }
        with pytest.raises(IntegrityError):
            await _insert_chunk(
                sessions,
                key,
                0,
                "tuple probe",
                _unit_vec(EMBEDDING_DIMENSIONS),
                **{column: overrides[column]},
            )


class TestServerSharedScope:
    @pytest.mark.anyio
    async def test_shared_scope_is_null_server_and_roundtrips(self, sessions) -> None:
        key = await _insert_document(sessions, DOC_SHARED, server_id=None)
        row = await _fetchone(
            sessions,
            """
            SELECT doc_id, server_id, app_id, namespace, revision, verification
            FROM knowledge_documents WHERE document_key = :key
            """,
            key=key,
        )
        assert row == (uuid.UUID(DOC_SHARED), None, None, "docs", 1, "unverified")

    @pytest.mark.anyio
    async def test_app_scope_requires_server(self, sessions) -> None:
        with pytest.raises(IntegrityError):
            await _insert_document(sessions, DOC_SHARED, app_id=APP_ID)

    @pytest.mark.anyio
    async def test_same_doc_id_lives_in_each_scope_independently(
        self, sessions
    ) -> None:
        """Explicit server/shared scope: the same doc_id can be indexed per
        server and in the shared namespace without collision."""
        shared_key = await _insert_document(sessions, DOC_SHARED, server_id=None)
        server_key = await _insert_document(
            sessions, DOC_SHARED, server_id=SERVER_A
        )
        assert shared_key != server_key
        count = await _fetchone(
            sessions,
            "SELECT count(*) FROM knowledge_documents WHERE doc_id = :doc",
            doc=uuid.UUID(DOC_SHARED),
        )
        assert count[0] == 2

    @pytest.mark.anyio
    async def test_server_filter_is_a_storage_boundary_join(self, sessions) -> None:
        """C07 rule 2: the server filter lives in the storage query — a
        server-scoped retrieval joined through the parent row sees only that
        server's chunks (shared chunks require an explicit NULL match)."""
        shared_key = await _insert_document(sessions, DOC_SHARED, server_id=None)
        key_a = await _insert_document(sessions, DOC_A, server_id=SERVER_A)
        key_b = await _insert_document(sessions, DOC_B, server_id=SERVER_B)
        await _insert_chunk(sessions, shared_key, 0, "shared chunk", _flat_vec(0.1))
        await _insert_chunk(sessions, key_a, 0, "server a chunk", _flat_vec(0.1))
        await _insert_chunk(sessions, key_b, 0, "server b chunk", _flat_vec(0.1))

        scoped = await _fetchall(
            sessions,
            """
            SELECT c.content FROM knowledge_doc_chunks c
            JOIN knowledge_documents d ON d.document_key = c.document_key
            WHERE d.server_id = :server
            ORDER BY c.content
            """,
            server=uuid.UUID(SERVER_A),
        )
        assert [r[0] for r in scoped] == ["server a chunk"]
        shared_only = await _fetchall(
            sessions,
            """
            SELECT c.content FROM knowledge_doc_chunks c
            JOIN knowledge_documents d ON d.document_key = c.document_key
            WHERE d.server_id IS NULL
            ORDER BY c.content
            """,
        )
        assert [r[0] for r in shared_only] == ["shared chunk"]


class TestFullTextSearch:
    @pytest.mark.anyio
    async def test_generated_tsvector_matches_and_gin_indexed(
        self, sessions, migrated_url: str
    ) -> None:
        key = await _insert_document(sessions, DOC_SHARED)
        await _insert_chunk(sessions, key, 0, "restart the postgres service", _flat_vec(0.1))
        await _insert_chunk(sessions, key, 1, "unrelated content entirely", _flat_vec(0.2))
        matches = await _fetchall(
            sessions,
            """
            SELECT ord FROM knowledge_doc_chunks
            WHERE search_vector @@ plainto_tsquery('simple', 'postgres service')
            ORDER BY ord
            """,
        )
        assert [r[0] for r in matches] == [0]
        with psycopg.connect(migrated_url) as conn:
            gin = conn.execute(
                """
                SELECT indexdef FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = 'knowledge_doc_chunks'
                  AND indexname = 'ix_knowledge_doc_chunks_search_vector'
                """
            ).fetchone()
        assert gin is not None and "USING gin" in gin[0]


# ---------------------------------------------------------------------------
# Acceptance 2 — revision uniqueness, tombstones, deletion relations, one DB
# ---------------------------------------------------------------------------


class TestSourceRevisionUniqueness:
    @pytest.mark.anyio
    async def test_duplicate_source_revision_is_an_integrity_violation(
        self, sessions
    ) -> None:
        """Re-applying one reindex intent is idempotent at the constraint
        level: a real duplicate key violation, never a silent second row."""
        await _insert_document(sessions, DOC_SHARED, revision=1)
        with pytest.raises(IntegrityError):
            await _insert_document(sessions, DOC_SHARED, revision=1)

    @pytest.mark.anyio
    async def test_same_revision_on_two_servers_is_not_a_collision(
        self, sessions
    ) -> None:
        await _insert_document(sessions, DOC_A, revision=1, server_id=SERVER_A)
        await _insert_document(sessions, DOC_A, revision=1, server_id=SERVER_B)
        count = await _fetchone(
            sessions, "SELECT count(*) FROM knowledge_documents"
        )
        assert count[0] == 2

    @pytest.mark.anyio
    async def test_two_live_revisions_of_one_source_rejected(self, sessions) -> None:
        """The live partial unique index allows exactly one live revision."""
        await _insert_document(sessions, DOC_SHARED, revision=1)
        with pytest.raises(IntegrityError):
            await _insert_document(sessions, DOC_SHARED, revision=2)

    @pytest.mark.anyio
    async def test_new_revision_lands_after_tombstone(self, sessions) -> None:
        """Tombstoned rows stay durable (history + rebuild input) while the
        next revision becomes the single live row."""
        await _insert_document(sessions, DOC_SHARED, revision=1, tombstone=True)
        key2 = await _insert_document(sessions, DOC_SHARED, revision=2)
        live = await _fetchall(
            sessions,
            """
            SELECT document_key FROM knowledge_documents
            WHERE doc_id = :doc AND NOT tombstone
            """,
            doc=uuid.UUID(DOC_SHARED),
        )
        assert [str(r[0]) for r in live] == [key2]


class TestTombstonesAndDeletionRelations:
    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("tombstone", "tombstoned_at"),
        [(True, None), (False, TS)],
        ids=["tombstone-without-timestamp", "timestamp-without-tombstone"],
    )
    async def test_tombstone_pair_is_enforced(
        self, sessions, tombstone: bool, tombstoned_at: str | None
    ) -> None:
        with pytest.raises(IntegrityError):
            await _insert_document_mismatch(sessions, tombstone, tombstoned_at)

    @pytest.mark.anyio
    async def test_chunk_cascade_is_the_deletion_relation(self, sessions) -> None:
        """ADR §4.3 rebuild at the storage layer: deleting the source row
        removes its derived chunks; re-insert (re-chunk/re-embed/re-index)
        works against the freed identity."""
        key = await _insert_document(sessions, DOC_SHARED, revision=1)
        await _insert_chunk(sessions, key, 0, "first chunk", _flat_vec(0.1))
        await _insert_chunk(sessions, key, 1, "second chunk", _flat_vec(0.2))
        await _exec(
            sessions,
            "DELETE FROM knowledge_documents WHERE document_key = :key",
            key=key,
        )
        leftovers = await _fetchone(sessions, "SELECT count(*) FROM knowledge_doc_chunks")
        assert leftovers[0] == 0
        # Rebuild: the same source identity accepts a fresh revision row and
        # fresh chunks after the purge.
        key2 = await _insert_document(sessions, DOC_SHARED, revision=1)
        await _insert_chunk(sessions, key2, 0, "re-embedded chunk", _flat_vec(0.3))
        row = await _fetchone(
            sessions,
            "SELECT content FROM knowledge_doc_chunks WHERE document_key = :key",
            key=key2,
        )
        assert row[0] == "re-embedded chunk"


async def _insert_document_mismatch(
    sessions, tombstone: bool, tombstoned_at: str | None
) -> None:
    await _exec(
        sessions,
        """
        INSERT INTO knowledge_documents
            (doc_id, server_id, app_id, namespace, revision, sha256,
             source_kind, origin, origin_revision, verification,
             source_path, observed_at, tombstone, tombstoned_at)
        VALUES
            (:doc_id, NULL, NULL, 'docs', 1, :sha256,
             'authored', 'operator', NULL, 'unverified',
             'shared/docs/x.md', :observed_at, :tombstone, :tombstoned_at)
        """,
        doc_id=uuid.uuid4(),
        sha256=SHA,
        observed_at=TS,
        tombstone=tombstone,
        tombstoned_at=tombstoned_at,
    )


class TestVocabularyPins:
    @pytest.mark.anyio
    async def test_namespace_vocabulary_rejects_unknown_values(self, sessions) -> None:
        with pytest.raises(IntegrityError):
            await _insert_document(sessions, DOC_SHARED, namespace="secrets")

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "column",
        ["source_kind", "verification"],
    )
    async def test_provenance_vocabularies_reject_unknown_values(
        self, sessions, column: str
    ) -> None:
        key = await _insert_document(sessions, DOC_SHARED)
        with pytest.raises(IntegrityError):
            await _exec(
                sessions,
                f"UPDATE knowledge_documents SET {column} = :v "
                "WHERE document_key = :k",
                v="not-a-frozen-value",
                k=key,
            )

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "sha256",
        ["AB" * 32, "ab" * 31, "ab" * 33, "gg" * 32],
        ids=["uppercase", "short", "long", "non-hex"],
    )
    async def test_sha256_must_be_canonical_hex(self, sessions, sha256: str) -> None:
        with pytest.raises(IntegrityError):
            await _insert_document(sessions, DOC_SHARED, sha256=sha256)


# ---------------------------------------------------------------------------
# Chain position, prior-head preservation, downgrade/upgrade round trip,
# and the single-database/no-surprise-store boundary
# ---------------------------------------------------------------------------


def test_migration_chain_is_linear_with_single_head(migrated_url: str) -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == [REVISION_IDS[-1]]
    revisions = {r.revision: r for r in script.walk_revisions()}
    assert sorted(revisions) == sorted(REVISION_IDS)
    assert revisions[REVISION_IDS[0]].down_revision is None
    for previous, following in zip(REVISION_IDS, REVISION_IDS[1:]):
        assert revisions[following].down_revision == previous


def test_migration_reupgrade_is_a_noop_and_records_the_revision(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAI_DATABASE_URL", migrated_url)
    command.upgrade(_alembic_config(), "head")  # second run on the same DB
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (REVISION_IDS[-1],)


@pytest.fixture(scope="session")
def prior_database_url(migrated_url: str) -> str:
    """Fixture database carried from the prior head (``0004``) to ``0005``.

    Feature-gate scenario fragment: upgrading a real database from the
    previous head must preserve prior-domain rows untouched.
    """
    url = migrated_url
    prior_db = "mai_fxt_07_prior"
    prior_url = _database_url(url, prior_db)
    with psycopg.connect(_database_url(url, "postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{prior_db}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{prior_db}"')
    previous = os.environ.get("MAI_DATABASE_URL")
    try:
        os.environ["MAI_DATABASE_URL"] = prior_url
        command.upgrade(_alembic_config(), "0004")
        server_id = uuid.UUID(SERVER_A)
        run_id = uuid.uuid4()
        with psycopg.connect(prior_url) as conn:
            conn.execute(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants)
                VALUES (%s, %s, 0, 1, 1, 'probe-agent', '["shell.exec"]')
                """,
                (run_id, server_id),
            )
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    yield prior_url
    with psycopg.connect(_database_url(url, "postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{prior_db}" WITH (FORCE)')


def test_upgrade_from_prior_head_preserves_prior_rows(
    prior_database_url: str,
) -> None:
    with psycopg.connect(prior_database_url) as conn:
        run = conn.execute(
            "SELECT count(*) FROM job_runs WHERE server_id = %s",
            (uuid.UUID(SERVER_A),),
        ).fetchone()
        knowledge_tables = conn.execute(
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('knowledge_documents', 'knowledge_doc_chunks')
            """
        ).fetchone()
        vector_extension = conn.execute(
            "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    assert run == (1,)
    assert knowledge_tables == (2,)
    assert vector_extension == (1,)


def test_downgrade_upgrades_cleanly(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Down/upgrade round trip of the new revision on an isolated database."""
    down_db = "mai_fxt_07_down"
    down_url = _database_url(migrated_url, down_db)
    with psycopg.connect(
        _database_url(migrated_url, "postgres"), autocommit=True
    ) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{down_db}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{down_db}"')
    previous = os.environ.get("MAI_DATABASE_URL")
    try:
        os.environ["MAI_DATABASE_URL"] = down_url
        command.upgrade(_alembic_config(), "head")
        with psycopg.connect(down_url) as conn:
            conn.execute(
                """
                INSERT INTO knowledge_documents
                    (doc_id, namespace, revision, sha256, source_kind,
                     origin, verification, source_path)
                VALUES (%s, 'docs', 1, %s, 'authored', 'operator',
                        'unverified', 'shared/docs/downgrade-probe.md')
                """,
                (uuid.UUID(DOC_SHARED), SHA),
            )
            conn.commit()
        command.downgrade(_alembic_config(), "0004")
        with psycopg.connect(down_url) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            leftovers = conn.execute(
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name LIKE 'knowledge\\_%'
                """
            ).fetchone()
            prior = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'job_runs'"
            ).fetchone()
        assert version == ("0004",)
        assert leftovers == (0,)
        assert prior == (1,)  # the P05 schema is untouched by our downgrade
        command.upgrade(_alembic_config(), "head")
        with psycopg.connect(down_url) as conn:
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == (REVISION_IDS[-1],)
            roundtrip = conn.execute(
                """
                SELECT count(*) FROM knowledge_documents d
                JOIN knowledge_doc_chunks c ON c.document_key = d.document_key
                WHERE d.doc_id = %s
                """,
                (uuid.UUID(DOC_SHARED),),
            ).fetchone()
        assert roundtrip == (0,)  # downgrade removed the data relation cleanly
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
        with psycopg.connect(
            _database_url(migrated_url, "postgres"), autocommit=True
        ) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{down_db}" WITH (FORCE)')


def test_public_schema_is_exactly_the_frozen_app_set(migrated_url: str) -> None:
    """One database, one app schema set: no second vector store, no
    engine-shadowing tables, no Hindsight-owned objects in this database."""
    with psycopg.connect(migrated_url) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                """
            )
        }
        schemata = {
            row[0]
            for row in conn.execute(
                "SELECT schema_name FROM information_schema.schemata"
            )
        }
        vector_extension = conn.execute(
            "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    assert tables == EXPECTED_APP_TABLES
    assert "dbos" not in schemata  # engine-owned schema, created at launch
    assert "hindsight" not in schemata  # Hindsight owns a separate database
    # The vectors live in THIS database — the extension is installed here,
    # not in a second store.
    assert vector_extension == (1,)


def test_source_record_columns_cover_the_c07_rule1_shape(
    migrated_url: str,
) -> None:
    with psycopg.connect(migrated_url) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'knowledge_documents'
                """
            )
        }
    for column in (
        "server_id",  # or explicit shared namespace (NULL)
        "app_id",  # optional
        "namespace",
        "source_path",  # path
        "source_kind",
        "revision",  # revision/hash pair
        "sha256",
        "origin",
        "origin_revision",  # origin/version
        "observed_at",  # observed/generated time (projection-owned)
        "verification",
        "tombstone",  # deletion relations
    ):
        assert column in columns, f"C07 rule-1 record column missing: {column}"
