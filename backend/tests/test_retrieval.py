"""Focused tests for server-filtered hybrid document retrieval (card 07-retrieval).

Acceptance (BRIEF):

1. Storage query enforces the server filter and explicit shared scope;
   citations/freshness returned.
2. Bounded relevant excerpts, not the whole corpus; unavailable index
   distinguished from zero results.

Boundary discipline (test policy, risk-critical slice): payload validation
and embedding-seam failure claims run offline (injected fakes); every
storage-boundary, scope-isolation, citation, ranking and bounded-excerpt
claim runs against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) migrated through the real linear Alembic chain,
indexed through the real ``07-chunk-index`` reconciler from a real
``DocumentStore`` data root — cross-server isolation, hybrid ranking and
stale-removal are proven by real SQL, not mocks. The capability/tool
integration claims run against the real ``03-capability-registry``,
``03-dispatcher``, ``03-policy`` and ``06-tool-bridge`` producers with the
``docs.retrieve`` descriptor registered — a payload with a smuggled target
field is denied by the real schema gate before any dispatch.

Feature-gate scenario (KB-FILTER, writer half): the scope-isolation tests
index *conflicting* documents for two fixture servers (same document
identity, different content) plus a shared document, then query through the
actual capability and prove the result sets are disjoint and the shared
namespace answers only to an explicit scope; citations resolve to the exact
source revision and survive a re-index. The assembled kill/restart journey
is owned by ``07-verify``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from mai.capabilities.dispatch import (
    CapabilityCall,
    CapabilityDispatcher,
    DispatchOutcome,
    ExecutorResult,
)
from mai.database import create_app_engine
from mai.knowledge.documents import DocumentStore
from mai.knowledge.index import (
    EMBEDDING_DIMENSIONS,
    ChunkEmbedder,
    IndexErrorCode,
    IndexingError,
    ScopeIndexer,
    chunk_markdown,
)
from mai.knowledge.retrieve import (
    CAPABILITY_ID,
    MAX_EXCERPT_CHARS,
    MAX_LIMIT,
    MAX_QUERY_CHARS,
    MIN_EXCERPT_CHARS,
    DocumentRetrievalCapability,
    Excerpt,
    RetrievalResult,
    RetrievalScope,
    RetrievalStatus,
    RetrieveError,
    RetrieveErrorCode,
    ScopeRetriever,
    descriptor,
    query_from_payload,
    result_document,
)
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Async storage tests run on the anyio pytest plugin (asyncio backend), the
# same discipline as tests/test_index.py and test_knowledge_schema.py.
pytestmark = pytest.mark.anyio

# Frozen fixture identities (canonical lowercase UUID strings, TEST-NET style).
SERVER_A = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
SERVER_B = "8f1c2e3d-4a5b-4c6d-9e7f-0a1b2c3d4e5f"
DOC_CONFLICT = "9d8c7b6a-5e4f-4a3b-2c1d-0e9f8a7b6c5d"
DOC_A_ONLY = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
DOC_B_ONLY = "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e"
DOC_SHARED = "3c4d5e6f-7a8b-4c9d-0e1f-2a3b4c5d6e7f"

PRINCIPAL = "admin-fixture"
RUN_1 = "7e6d5c4b-3a2b-4c1d-8e7f-0a1b2c3d4e5f"

TEXT_CONFLICT_A = (
    "# Conflict procedure\n\nThe alfasecret-a restart order applies to "
    "server A only: drain, restart, verify.\n"
)
TEXT_CONFLICT_B = (
    "# Conflict procedure\n\nThe bravosecret-b restart order applies to "
    "server B only: stop, reimage, verify.\n"
)
TEXT_A_ONLY = (
    "# Server A notes\n\nalfaunique-a maintenance window on tuesday.\n"
)
TEXT_B_ONLY = (
    "# Server B notes\n\nbravounique-b maintenance window on friday.\n"
)
TEXT_SHARED = (
    "# Shared runbook\n\nsharedgamma procedure valid for every server.\n"
)


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0005``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the retrieval claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "DROP TABLE IF EXISTS knowledge_doc_chunks, knowledge_documents, "
                "job_schedule_fires, job_schedules, "
                "job_events, job_steps, job_runs, approvals, "
                "profile_projection_journal, profile_projection, admin_sessions, "
                "admin_credential, alembic_version"
            )
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture(scope="session")
def noindex_url(migrated_url: str) -> str:
    """A second database on the same fixture server WITHOUT the schema.

    Proves ``index_unavailable`` is a real storage-boundary outcome, not a
    stand-in for zero results.
    """
    admin = migrated_url.rsplit("/", 1)[0] + "/postgres"
    name = "mai_noindex_07retrieval"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield migrated_url.rsplit("/", 1)[0] + "/" + name
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def sessions(migrated_url: str):
    """The async sessionmaker factory; cleanup runs on a throwaway loop.

    Deliberately a *sync* fixture: the two CLI-subprocess tests are sync
    and need the same factory. Async tests receive the factory unchanged.
    """
    engine, factory = create_app_engine(migrated_url)
    yield factory

    async def _clean() -> None:
        async with factory() as session:
            await session.execute(text("DELETE FROM knowledge_doc_chunks"))
            await session.execute(text("DELETE FROM knowledge_documents"))
            await session.commit()
        await engine.dispose()

    asyncio.run(_clean())


@pytest.fixture
def store(tmp_path: Path) -> DocumentStore:
    return DocumentStore(tmp_path / "docs-root")


class _HashedEmbedder(ChunkEmbedder):
    """Deterministic offline embedder: one unit vector per text content.

    The basis dimension derives from the text hash, so a test controls the
    vector-neighborhood of any string exactly (querying with a chunk's own
    content gives cosine distance 0 to that chunk and 1 to every other).
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        wrong_dims: bool = False,
        overrides: dict[str, int] | None = None,
    ) -> None:
        self._fail = fail
        self._wrong_dims = wrong_dims
        self._overrides = dict(overrides or {})
        self.requests: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._fail:
            raise IndexingError(
                IndexErrorCode.EMBEDDING_UNAVAILABLE, "fixture endpoint down"
            )
        self.requests.append(list(texts))
        vectors = []
        for item in texts:
            if self._wrong_dims:
                vectors.append([0.0] * 64)
                continue
            dim = self._overrides.get(
                item,
                int.from_bytes(
                    hashlib.sha256(item.encode("utf-8")).digest()[:8]
                )
                % EMBEDDING_DIMENSIONS,
            )
            vector = [0.0] * EMBEDDING_DIMENSIONS
            vector[dim] = 1.0
            vectors.append(vector)
        return vectors


class _BoomEmbedder(ChunkEmbedder):
    """Unstructured failure: must be classified, never leak a message."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("fixture boom with secret-ish detail")


def _run(coro):
    """Run one coroutine on a throwaway loop (sync test helpers)."""
    return asyncio.run(coro)


def _retriever(sessions, embedder: ChunkEmbedder) -> ScopeRetriever:
    return ScopeRetriever(sessions, embedder)


async def _write_and_index(
    store: DocumentStore,
    sessions,
    embedder: ChunkEmbedder,
    doc_id: str,
    text: str,
    *,
    server_id: str | None = None,
    revision: int = 0,
):
    """Write one canonical doc and reconcile its intent into the projection."""
    written = store.write(
        uuid.UUID(doc_id),
        text,
        server_id=uuid.UUID(server_id) if server_id else None,
        expected_revision=revision,
        origin="test-fixture",
    )
    indexer = ScopeIndexer(sessions, store, embedder)
    outcomes = await indexer.reconcile_scope(
        server_id=uuid.UUID(server_id) if server_id else None
    )
    assert all(
        outcome.status.value in ("indexed", "unchanged") for outcome in outcomes
    ), outcomes
    return written


async def _seed_two_servers(store: DocumentStore, sessions, embedder: ChunkEmbedder):
    """KB-FILTER fixture: conflicting docs on two servers + a shared doc."""
    await _write_and_index(store, sessions, embedder, DOC_CONFLICT, TEXT_CONFLICT_A, server_id=SERVER_A)
    await _write_and_index(store, sessions, embedder, DOC_CONFLICT, TEXT_CONFLICT_B, server_id=SERVER_B)
    await _write_and_index(store, sessions, embedder, DOC_A_ONLY, TEXT_A_ONLY, server_id=SERVER_A)
    await _write_and_index(store, sessions, embedder, DOC_B_ONLY, TEXT_B_ONLY, server_id=SERVER_B)
    await _write_and_index(store, sessions, embedder, DOC_SHARED, TEXT_SHARED)


# ---------------------------------------------------------------------------
# Payload validation (offline, defense in depth behind the bridge gate)
# ---------------------------------------------------------------------------


def test_query_from_payload_defaults_to_context_server_scope() -> None:
    request = query_from_payload({"query": "restart procedure"})
    assert request.query == "restart procedure"
    assert request.scope is RetrievalScope.SERVER
    assert request.namespace is None
    assert request.limit == 5
    assert request.max_excerpt_chars == 1200


def test_query_from_payload_shared_scope_is_explicit() -> None:
    request = query_from_payload(
        {"query": "x", "scope": "shared", "namespace": "runbooks", "limit": 3}
    )
    assert request.scope is RetrievalScope.SHARED
    assert request.namespace.value == "runbooks"
    assert request.limit == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},  # query missing
        {"query": ""},  # empty
        {"query": "   "},  # whitespace only
        {"query": "a" * (MAX_QUERY_CHARS + 1)},  # too long
        {"query": "re\x00start"},  # NUL byte
        {"query": "x", "scope": "shared "},  # near-miss scope
        {"query": "x", "scope": "SHARED"},  # case-sensitive vocabulary
        {"query": "x", "scope": "other-server"},  # no such scope
        {"query": "x", "namespace": "secrets"},  # frozen vocabulary only
        {"query": "x", "limit": 0},
        {"query": "x", "limit": MAX_LIMIT + 1},
        {"query": "x", "limit": True},  # bool is not an int
        {"query": "x", "limit": "5"},
        {"query": "x", "max_excerpt_chars": MIN_EXCERPT_CHARS - 1},
        {"query": "x", "max_excerpt_chars": MAX_EXCERPT_CHARS + 1},
        {"query": "x", "server_id": SERVER_B},  # smuggled target field
        {"query": "x", "app_id": RUN_1},
        {"query": "x", "unexpected": 1},
    ],
)
def test_query_from_payload_rejects_out_of_contract(payload) -> None:
    with pytest.raises(RetrieveError) as excinfo:
        query_from_payload(payload)
    assert excinfo.value.code is RetrieveErrorCode.INVALID_PAYLOAD


def test_query_from_payload_rejects_non_mapping() -> None:
    with pytest.raises(RetrieveError):
        query_from_payload(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Embedding seam failures (offline): unavailable, never zero results
# ---------------------------------------------------------------------------


async def test_embedding_failure_is_unavailable_not_zero(sessions) -> None:
    retriever = _retriever(sessions, _HashedEmbedder(fail=True))
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="anything"
    )
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.code is RetrieveErrorCode.EMBEDDING_UNAVAILABLE
    assert result.excerpts == ()
    assert result.error_type == "IndexingError"
    document = result_document(result)
    assert document["excerpts"] == []
    assert "detail" not in document  # value-free: no exception text


async def test_unstructured_embedder_failure_is_classified(sessions) -> None:
    retriever = _retriever(sessions, _BoomEmbedder())
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="anything"
    )
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.code is RetrieveErrorCode.EMBEDDING_UNAVAILABLE
    assert result.error_type == "RuntimeError"
    assert "boom" not in json.dumps(result_document(result))


async def test_dimension_mismatch_refused_not_compared(sessions) -> None:
    retriever = _retriever(sessions, _HashedEmbedder(wrong_dims=True))
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="anything"
    )
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.code is RetrieveErrorCode.DIMENSION_MISMATCH
    assert result.excerpts == ()


# ---------------------------------------------------------------------------
# KB-FILTER: storage-boundary scope isolation with conflicting documents
# ---------------------------------------------------------------------------


async def test_server_scope_returns_only_own_chunks(
    store, sessions
) -> None:
    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    retriever = _retriever(sessions, embedder)

    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="alfasecret-a restart order"
    )
    assert result.status is RetrievalStatus.OK
    assert result.scope == "server"
    assert result.server_id == uuid.UUID(SERVER_A)
    assert result.excerpts, "expected server-A matches"
    # The scope filter is a storage predicate: only server-A documents can
    # appear at all (both fixture docs of server A may match lexically).
    assert all(
        str(excerpt.doc_id) in (DOC_CONFLICT, DOC_A_ONLY)
        for excerpt in result.excerpts
    )
    joined = "\n".join(excerpt.excerpt for excerpt in result.excerpts)
    assert "alfasecret-a" in joined
    assert "bravosecret-b" not in joined
    assert "sharedgamma" not in joined


async def test_cross_server_result_sets_are_disjoint(store, sessions) -> None:
    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    retriever = _retriever(sessions, embedder)

    result_a = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="restart procedure verify"
    )
    result_b = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_B), query="restart procedure verify"
    )
    assert result_a.status is RetrievalStatus.OK
    assert result_b.status is RetrievalStatus.OK
    citations_a = {
        (str(excerpt.doc_id), excerpt.revision, excerpt.sha256, excerpt.ord)
        for excerpt in result_a.excerpts
    }
    citations_b = {
        (str(excerpt.doc_id), excerpt.revision, excerpt.sha256, excerpt.ord)
        for excerpt in result_b.excerpts
    }
    assert citations_a and citations_b
    assert citations_a.isdisjoint(citations_b)
    joined_a = "\n".join(e.excerpt for e in result_a.excerpts)
    joined_b = "\n".join(e.excerpt for e in result_b.excerpts)
    assert "bravosecret-b" not in joined_a
    assert "alfasecret-a" not in joined_b


async def test_shared_scope_answers_only_shared_namespace(
    store, sessions
) -> None:
    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    retriever = _retriever(sessions, embedder)

    shared = await retriever.retrieve(server_id=None, query="sharedgamma")
    assert shared.status is RetrievalStatus.OK
    assert shared.scope == "shared"
    assert shared.server_id is None
    assert shared.excerpts
    assert all(
        excerpt.doc_id == uuid.UUID(DOC_SHARED) for excerpt in shared.excerpts
    )

    # The default server scope never leaks shared-namespace rows.
    server_a = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="sharedgamma procedure"
    )
    assert all(
        excerpt.doc_id != uuid.UUID(DOC_SHARED) for excerpt in server_a.excerpts
    )


async def test_shared_namespace_rows_hidden_from_server_scope(store, sessions) -> None:
    """server_id IS NULL rows can never satisfy a server scope predicate.

    The vector channel is k-NN, so a weakly-related server-scoped chunk
    may still surface; the control is that the shared-namespace document
    itself never leaks into a server scope.
    """
    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    retriever = _retriever(sessions, embedder)
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="sharedgamma", limit=MAX_LIMIT
    )
    assert result.status is RetrievalStatus.OK
    assert uuid.UUID(DOC_SHARED) not in {
        excerpt.doc_id for excerpt in result.excerpts
    }


# ---------------------------------------------------------------------------
# Citations and freshness (C07 rule 4)
# ---------------------------------------------------------------------------


async def test_citations_resolve_to_source_revision(store, sessions) -> None:
    embedder = _HashedEmbedder()
    written = await _write_and_index(
        store, sessions, embedder, DOC_A_ONLY, TEXT_A_ONLY, server_id=SERVER_A
    )
    retriever = _retriever(sessions, embedder)
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="alfaunique-a"
    )
    assert result.excerpts
    excerpt = result.excerpts[0]
    assert isinstance(excerpt, Excerpt)
    assert excerpt.doc_id == uuid.UUID(DOC_A_ONLY)
    assert excerpt.revision == written.revision == 1
    assert excerpt.sha256 == written.sha256
    assert excerpt.namespace == "docs"
    assert excerpt.ord == 0
    assert excerpt.verification == "unverified"
    assert excerpt.observed_at is not None
    document = result_document(result)
    rendered = document["excerpts"][0]
    assert rendered["citation"] == f"docs:{DOC_A_ONLY}#r1:0"
    assert rendered["document_key"]
    assert rendered["observed_at"]


async def test_citations_follow_source_update(store, sessions) -> None:
    embedder = _HashedEmbedder()
    await _write_and_index(
        store, sessions, embedder, DOC_A_ONLY, TEXT_A_ONLY, server_id=SERVER_A
    )
    updated = store.write(
        uuid.UUID(DOC_A_ONLY),
        TEXT_A_ONLY + "\n## Addendum\n\nalfaunique-a second revision note.\n",
        server_id=uuid.UUID(SERVER_A),
        expected_revision=1,
        origin="test-fixture",
    )
    indexer = ScopeIndexer(sessions, store, embedder)
    await indexer.reconcile_scope(server_id=uuid.UUID(SERVER_A))
    retriever = _retriever(sessions, embedder)
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="alfaunique-a"
    )
    assert result.excerpts
    revisions = {excerpt.revision for excerpt in result.excerpts}
    assert revisions == {updated.revision}
    assert {excerpt.sha256 for excerpt in result.excerpts} == {updated.sha256}
    # Only one live revision row exists (stale revision's chunks were
    # cascade-removed by the re-index).
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM knowledge_documents "
                    "WHERE doc_id = :doc_id AND tombstone = false"
                ),
                {"doc_id": DOC_A_ONLY},
            )
        ).scalar()
    assert rows == 1


async def test_delete_removes_stale_results(store, sessions) -> None:
    embedder = _HashedEmbedder()
    written = await _write_and_index(
        store, sessions, embedder, DOC_A_ONLY, TEXT_A_ONLY, server_id=SERVER_A
    )
    store.delete(
        uuid.UUID(DOC_A_ONLY),
        server_id=uuid.UUID(SERVER_A),
        expected_revision=written.revision,
    )
    indexer = ScopeIndexer(sessions, store, embedder)
    await indexer.reconcile_scope(server_id=uuid.UUID(SERVER_A))
    retriever = _retriever(sessions, embedder)
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="alfaunique-a"
    )
    assert result.status is RetrievalStatus.OK
    assert not result.excerpts


# ---------------------------------------------------------------------------
# Unavailable index vs zero results (real storage boundary)
# ---------------------------------------------------------------------------


async def test_zero_results_is_ok_not_unavailable(store, sessions) -> None:
    """An empty scope is a real zero, distinct from an unavailable index.

    The vector channel is k-NN semantics (a non-matching query returns
    the closest chunks of a non-empty scope), so zero results is defined
    by the scope: a server with no indexed documents.
    """
    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    retriever = _retriever(sessions, embedder)
    empty_server = uuid.UUID("4d5e6f7a-8b9c-4d0e-9f1a-2b3c4d5e6f7a")
    result = await retriever.retrieve(
        server_id=empty_server,
        query="no-such-term-xyzqrs anywhere zzz",
    )
    assert result.status is RetrievalStatus.OK
    assert result.code is None
    assert not result.excerpts
    assert result.error_type is None
    # A real query ran: the embedding seam saw exactly the query text
    # (index-time chunk batches preceded it).
    assert embedder.requests[-1] == ["no-such-term-xyzqrs anywhere zzz"]


async def test_unavailable_index_is_distinct_from_zero(
    store, sessions, noindex_url: str
) -> None:
    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    engine, factory = create_app_engine(noindex_url)
    try:
        retriever = ScopeRetriever(factory, embedder)
        result = await retriever.retrieve(
            server_id=uuid.UUID(SERVER_A), query="alfasecret-a"
        )
    finally:
        await engine.dispose()
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.code is RetrieveErrorCode.INDEX_UNAVAILABLE
    assert result.excerpts == ()
    assert result.error_type
    document = result_document(result)
    assert document["status"] == "unavailable"
    assert document["code"] == "index_unavailable"
    assert "detail" not in document  # value-free: no SQL error text


# ---------------------------------------------------------------------------
# Hybrid ranking: both signals contribute (real SQL, real index)
# ---------------------------------------------------------------------------


async def test_hybrid_combines_vector_and_fts_signals(store, sessions) -> None:
    # Fixture geometry (unit-basis filler vectors, dimension overrides):
    #   DOC_A_ONLY — vector-signal-only: its chunk shares the query
    #     embedding dimension but contains no FTS lexeme.
    #   DOC_B_ONLY — FTS-signal-only: contains the query lexeme, distant
    #     embedding.
    #   DOC_SHARED — dual signal: query lexeme AND query embedding
    #     dimension (unique distance 0 → strict vector rank 1).
    text_vector_only = "# Vector doc\n\nzqv dimension probe text\n"
    text_fts_only = "# Fts doc\n\nneedleterm fts-only hit\n"
    text_both = "# Combined doc\n\nneedleterm combined hit\n"
    combined_chunk = chunk_markdown(text_both)[0].content
    embedder = _HashedEmbedder(
        overrides={combined_chunk: 100, "needleterm": 100}
    )
    await _write_and_index(
        store, sessions, embedder, DOC_A_ONLY, text_vector_only,
        server_id=SERVER_A,
    )
    await _write_and_index(
        store, sessions, embedder, DOC_B_ONLY, text_fts_only,
        server_id=SERVER_A,
    )
    await _write_and_index(
        store, sessions, embedder, DOC_SHARED, text_both, server_id=SERVER_A
    )
    retriever = _retriever(sessions, embedder)
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A),
        query="needleterm",
        limit=MAX_LIMIT,
    )
    assert result.status is RetrievalStatus.OK
    by_doc = {str(excerpt.doc_id): excerpt for excerpt in result.excerpts}
    assert set(by_doc) == {DOC_A_ONLY, DOC_B_ONLY, DOC_SHARED}

    vector_only = by_doc[DOC_A_ONLY]
    fts_only = by_doc[DOC_B_ONLY]
    combined = by_doc[DOC_SHARED]

    # Channel attribution: each single-signal doc surfaced through exactly
    # its own signal (the vector pool has no lexical filter, so an FTS hit
    # also carries an arbitrary equidistant vector rank; the FTS channel
    # is what is asserted).
    assert combined.vector_rank == 1  # unique distance 0
    assert combined.fts_rank is not None
    assert vector_only.fts_rank is None
    assert fts_only.fts_rank is not None

    # The dual-signal chunk outranks the pure-vector chunk; the pure-FTS
    # chunk outranks the pure-vector chunk too (both contributions beat
    # one). NB: combined-vs-fts-only strict order is deliberately not
    # asserted: equidistant filler vectors make a (1,2)-vs-(2,1) RRF tie
    # possible by construction.
    assert combined.score > vector_only.score
    assert fts_only.score > vector_only.score
    assert combined.excerpt == combined_chunk[: len(combined.excerpt)]


async def test_vector_signal_alone_can_rank(store, sessions) -> None:
    """Without the FTS term, the vector pool still surfaces its chunk."""
    embedder = _HashedEmbedder()
    probe = chunk_markdown("# Probe\n\nzqv dimension probe text\n")[0].content
    await _write_and_index(
        store,
        sessions,
        embedder,
        DOC_A_ONLY,
        "# Probe\n\nzqv dimension probe text\n",
        server_id=SERVER_A,
    )
    await _write_and_index(
        store,
        sessions,
        embedder,
        DOC_B_ONLY,
        "# Other\n\ncompletely unrelated words here\n",
        server_id=SERVER_A,
    )
    retriever = _retriever(sessions, embedder)
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query=probe
    )
    assert result.status is RetrievalStatus.OK
    assert result.excerpts
    assert result.excerpts[0].doc_id == uuid.UUID(DOC_A_ONLY)
    assert result.excerpts[0].vector_rank == 1


# ---------------------------------------------------------------------------
# Bounded excerpts, not the corpus (acceptance 2)
# ---------------------------------------------------------------------------


async def test_excerpt_budget_truncates_but_citation_survives(
    store, sessions
) -> None:
    embedder = _HashedEmbedder()
    big_text = (
        "# Large doc\n\nalfaunique-a " + ("filler-word " * 700) + "\n"
    )
    written = await _write_and_index(
        store, sessions, embedder, DOC_A_ONLY, big_text, server_id=SERVER_A
    )
    retriever = _retriever(sessions, embedder)
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A),
        query="alfaunique-a",
        max_excerpt_chars=MIN_EXCERPT_CHARS,
    )
    assert result.excerpts
    excerpt = result.excerpts[0]
    assert len(excerpt.excerpt) <= MIN_EXCERPT_CHARS
    assert excerpt.truncated is True
    assert len(big_text) > MIN_EXCERPT_CHARS
    # Traceability survives the cut: full citation tuple intact.
    assert excerpt.doc_id == uuid.UUID(DOC_A_ONLY)
    assert excerpt.revision == written.revision
    assert excerpt.sha256 == written.sha256
    document = result_document(result)
    assert document["excerpts"][0]["truncated"] is True


async def test_limit_bounds_excerpt_count(store, sessions) -> None:
    embedder = _HashedEmbedder()
    for index, doc_id in enumerate((DOC_A_ONLY, DOC_B_ONLY, DOC_SHARED)):
        await _write_and_index(
            store,
            sessions,
            embedder,
            doc_id,
            f"# Doc {index}\n\nneedleterm bounded corpus probe {index}\n",
            server_id=SERVER_A,
        )
    retriever = _retriever(sessions, embedder)
    result = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A), query="needleterm", limit=2
    )
    assert result.status is RetrievalStatus.OK
    assert 1 <= len(result.excerpts) <= 2


async def test_namespace_filter_bounds_collections(store, sessions) -> None:
    from mai.knowledge.documents import DocumentNamespace

    embedder = _HashedEmbedder()
    await _write_and_index(
        store, sessions, embedder, DOC_A_ONLY,
        "# Doc\n\nneedleterm in the docs collection\n",
        server_id=SERVER_A,
    )
    store.write(
        uuid.UUID(DOC_B_ONLY),
        "# Runbook\n\nneedleterm in the runbooks collection\n",
        server_id=uuid.UUID(SERVER_A),
        namespace=DocumentNamespace.RUNBOOKS,
        expected_revision=0,
        origin="test-fixture",
    )
    indexer = ScopeIndexer(sessions, store, embedder)
    await indexer.reconcile_scope(server_id=uuid.UUID(SERVER_A))
    retriever = _retriever(sessions, embedder)
    docs = await retriever.retrieve(
        server_id=uuid.UUID(SERVER_A),
        query="needleterm",
        namespace=DocumentNamespace.RUNBOOKS,
        limit=MAX_LIMIT,
    )
    assert docs.excerpts
    assert all(
        excerpt.namespace == "runbooks" for excerpt in docs.excerpts
    )


# ---------------------------------------------------------------------------
# Capability / tool integration (real registry, dispatcher, policy, bridge)
# ---------------------------------------------------------------------------


def _registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(descriptor())
    return registry


def _grants(*servers: str):
    return {
        CAPABILITY_ID: frozenset(uuid.UUID(server) for server in servers)
    }


def _policy(revision: int = 7, denied: tuple[str, ...] = ()) -> Policy:
    return Policy(revision=revision, denied_capabilities=frozenset(denied))


def _context(
    *,
    server_id: str = SERVER_A,
    allowed: tuple[str, ...] = (CAPABILITY_ID,),
    mode: PolicyMode = PolicyMode.DELEGATED,
) -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=uuid.UUID(server_id),
        run_id=uuid.UUID(RUN_1),
        parent_scope=None,
        profile_revision="prof-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=allowed,
        mode=mode,
    )


def _unused_approvals() -> ApprovalStore:
    """A real store over a lazily-connecting engine (never dialed)."""
    _, sessions = create_app_engine("postgresql://unused:unused@127.0.0.1:1/unused")
    return ApprovalStore(sessions)


class _RecordingCapability:
    """Wraps the real executor, recording invocations for assertions."""

    def __init__(self, capability: DocumentRetrievalCapability) -> None:
        self.capability = capability
        self.calls: list[CapabilityCall] = []

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        self.calls.append(call)
        return await self.capability(call)


def _dispatcher(
    registry: CapabilityRegistry,
    executor,
    *,
    grants=None,
    policy=None,
) -> CapabilityDispatcher:
    return CapabilityDispatcher(
        registry=registry,
        approvals=_unused_approvals(),
        grants=(lambda: grants) if grants is not None else (lambda: {}),
        policy=policy if policy is not None else (lambda: _policy()),
        adopted=lambda server_id: True,
        credentials=lambda: None,
        executor=executor,
        output_budget_bytes=64 * 1024,
    )


async def test_dispatcher_executes_granted_scope_query(store, sessions) -> None:
    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    capability = _RecordingCapability(
        DocumentRetrievalCapability(_retriever(sessions, embedder))
    )
    registry = _registry()
    dispatcher = _dispatcher(registry, capability, grants=_grants(SERVER_A))
    outcome: DispatchOutcome = await dispatcher.dispatch(
        context=_context(server_id=SERVER_A),
        capability_id=CAPABILITY_ID,
        payload={"query": "alfasecret-a restart order"},
    )
    assert outcome.allowed is True
    assert outcome.effect == "executed"
    assert outcome.executor_ok is True
    assert outcome.approval_id is None  # read effect needs no approval
    assert len(capability.calls) == 1
    parsed = json.loads(outcome.output.text)
    assert parsed["status"] == "ok"
    assert parsed["scope"] == "server"
    assert parsed["server_id"] == SERVER_A
    assert parsed["excerpts"]
    joined = "\n".join(item["excerpt"] for item in parsed["excerpts"])
    assert "bravosecret-b" not in joined
    assert parsed["excerpts"][0]["citation"]


async def test_dispatcher_denies_server_outside_grant_scope(
    store, sessions
) -> None:
    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    capability = _RecordingCapability(
        DocumentRetrievalCapability(_retriever(sessions, embedder))
    )
    registry = _registry()
    dispatcher = _dispatcher(registry, capability, grants=_grants(SERVER_A))
    outcome = await dispatcher.dispatch(
        context=_context(server_id=SERVER_B),
        capability_id=CAPABILITY_ID,
        payload={"query": "bravosecret-b restart order"},
    )
    assert outcome.allowed is False
    assert capability.calls == []
    assert outcome.effect == "not_attempted"


async def test_policy_deny_list_blocks_retrieval(store, sessions) -> None:
    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    capability = _RecordingCapability(
        DocumentRetrievalCapability(_retriever(sessions, embedder))
    )
    registry = _registry()
    dispatcher = _dispatcher(
        registry,
        capability,
        grants=_grants(SERVER_A),
        policy=lambda: _policy(denied=(CAPABILITY_ID,)),
    )
    outcome = await dispatcher.dispatch(
        context=_context(server_id=SERVER_A),
        capability_id=CAPABILITY_ID,
        payload={"query": "alfasecret-a"},
    )
    assert outcome.allowed is False
    assert capability.calls == []


async def test_granted_capability_requires_allowed_listing(store, sessions) -> None:
    from mai.agents.tools import CapabilityToolBridge

    embedder = _HashedEmbedder()
    registry = _registry()
    capability = DocumentRetrievalCapability(_retriever(sessions, embedder))
    dispatcher = _dispatcher(registry, capability, grants=_grants(SERVER_A))
    exposed = CapabilityToolBridge(
        registry=registry,
        dispatcher=dispatcher,
        context=_context(server_id=SERVER_A, allowed=(CAPABILITY_ID,)),
    )
    names = {tool.name for tool in exposed.tools}
    assert any("retrieve" in name for name in names)
    hidden = CapabilityToolBridge(
        registry=registry,
        dispatcher=dispatcher,
        context=_context(server_id=SERVER_A, allowed=()),
    )
    assert {tool.name for tool in hidden.tools} == set()


async def test_bridge_schema_gate_denies_smuggled_target_field(
    store, sessions
) -> None:
    """A payload naming another server is denied before any dispatch."""
    from mai.agents.tools import CapabilityToolBridge

    embedder = _HashedEmbedder()
    await _seed_two_servers(store, sessions, embedder)
    capability = _RecordingCapability(
        DocumentRetrievalCapability(_retriever(sessions, embedder))
    )
    registry = _registry()
    dispatcher = _dispatcher(registry, capability, grants=_grants(SERVER_A))
    bridge = CapabilityToolBridge(
        registry=registry,
        dispatcher=dispatcher,
        context=_context(server_id=SERVER_A),
    )
    tool = next(t for t in bridge.tools if "retrieve" in t.name)
    result = json.loads(
        await tool.function(
            query="bravosecret-b",
            scope="server",
            server_id=SERVER_B,
        )
    )
    assert result["status"] == "denied"
    assert result["error"]["code"] == "payload_schema_mismatch"
    assert result["reason"] == "payload_schema_mismatch"
    assert capability.calls == []
    assert result["effect"] == "not_attempted"


async def test_capability_executor_validates_direct_calls(store, sessions) -> None:
    embedder = _HashedEmbedder()
    capability = DocumentRetrievalCapability(_retriever(sessions, embedder))
    call = CapabilityCall(
        descriptor=descriptor(),
        payload={"query": "", "scope": "server"},
        context=_context(server_id=SERVER_A),
        credentials={},
        op_id=uuid.uuid4(),
    )
    with pytest.raises(RetrieveError) as excinfo:
        await capability(call)
    assert excinfo.value.code is RetrieveErrorCode.INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# Production entrypoint: real subprocess against the fixture database
# ---------------------------------------------------------------------------


class _EmbeddingHandler(BaseHTTPRequestHandler):
    """Local OpenAI-compatible /v1/embeddings fixture (deterministic)."""

    embedder = _HashedEmbedder()

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        if self.path != "/v1/embeddings":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        vectors = _run(self.embedder.embed(list(body["input"])))
        payload = json.dumps(
            {
                "object": "list",
                "data": [
                    {"index": i, "embedding": v}
                    for i, v in enumerate(vectors)
                ],
                "model": body["model"],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # noqa: D102 - quiet fixture
        pass


@pytest.fixture
def embedding_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def test_cli_query_entrypoint_real_subprocess(
    store, sessions, migrated_url: str, embedding_server: str
) -> None:
    """``python -m mai.knowledge.retrieve query`` against the real fixture."""
    embedder = _HashedEmbedder()
    _run(_seed_two_servers(store, sessions, embedder))
    env = dict(os.environ, MAI_DATABASE_URL=migrated_url)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mai.knowledge.retrieve",
            "query",
            "--server",
            SERVER_A,
            "--query",
            "alfasecret-a restart order",
            "--base-url",
            embedding_server,
            "--limit",
            "3",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=BACKEND_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    document = lines[0]
    assert document["status"] == "ok"
    assert document["scope"] == "server"
    assert document["server_id"] == SERVER_A
    assert len(document["excerpts"]) <= 3
    joined = "\n".join(item["excerpt"] for item in document["excerpts"])
    assert "alfasecret-a" in joined
    assert "bravosecret-b" not in joined
    assert proc.stderr == ""


def test_cli_shared_scope_requires_explicit_flag(
    store, sessions, migrated_url: str, embedding_server: str
) -> None:
    embedder = _HashedEmbedder()
    _run(_seed_two_servers(store, sessions, embedder))
    env = dict(os.environ, MAI_DATABASE_URL=migrated_url)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mai.knowledge.retrieve",
            "query",
            "--shared",
            "--query",
            "sharedgamma",
            "--base-url",
            embedding_server,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=BACKEND_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout.strip().splitlines()[-1])
    assert document["status"] == "ok"
    assert document["scope"] == "shared"
    assert document["server_id"] is None
    assert {item["doc_id"] for item in document["excerpts"]} == {DOC_SHARED}
