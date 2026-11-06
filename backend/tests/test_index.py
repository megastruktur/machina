"""Focused tests for section-aware chunking + idempotent reindexing (card 07-chunk-index).

Acceptance (BRIEF):

1. Stable revision/offset chunks with embedding model identity; retries do
   not duplicate current source chunks.
2. Update/delete removes stale retrieval results; unavailable embedding
   leaves visible pending index, not empty-success.

Boundary discipline (test policy, risk-critical slice): chunking and
embedder-shape claims run offline (mock transport — the exact pinned
``openai`` SDK + ``httpx2`` surface, never dialed); every storage,
idempotency and deletion claim runs against a real disposable PostgreSQL
fixture (``MAI_TEST_DATABASE_URL``) migrated through the real linear
Alembic chain, reconciling the real doc-store outbox from a real
``DocumentStore`` data root — retry/crash windows are proven by real
re-runs, stale-removal by real FTS queries, the dimension tuple by real
``vector_dims()`` results. Fixtures carry only TEST-NET-style fixed UUIDs
and deterministic filler vectors — never secret values.

Feature-gate scenario writer half (KB-FILTER): the scope-isolation tests
here index the same document identity under two server scopes plus the
shared namespace and verify per-scope projection rows at the storage
boundary; the assembled kill/restart journey is owned by ``07-verify``.
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

from mai.database import create_app_engine
from mai.knowledge.documents import DocumentStore
from mai.knowledge.index import (
    CREDENTIAL_FENCE_PLACEHOLDER,
    DISTANCE_METRIC,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    MAX_CHUNK_CHARS,
    ChunkEmbedder,
    GatewayEmbedder,
    IndexErrorCode,
    IndexingError,
    OutcomeStatus,
    ScopeIndexer,
    chunk_markdown,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Async storage tests run on the anyio pytest plugin (asyncio backend), the
# same discipline as tests/test_knowledge_schema.py; sync chunking/embedder
# tests are unaffected by the marker.
pytestmark = pytest.mark.anyio

# Frozen fixture identities (canonical lowercase UUID strings, TEST-NET style).
SERVER_A = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
SERVER_B = "8f1c2e3d-4a5b-4c6d-9e7f-0a1b2c3d4e5f"
DOC_SHARED = "9d8c7b6a-5e4f-4a3b-2c1d-0e9f8a7b6c5d"
DOC_A = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"

DIMENSION_CONTRACT = (
    EMBEDDING_PROVIDER,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSIONS,
    DISTANCE_METRIC,
)


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0005``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the reconcile claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            # Whole-schema reset (D09): FK-safe and immune to future revisions
            # (the enumerated drop list went stale across migrations).
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
            conn.commit()
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
    async with factory() as session:
        await session.execute(text("DELETE FROM knowledge_doc_chunks"))
        await session.execute(text("DELETE FROM knowledge_documents"))
        await session.commit()
    await engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> DocumentStore:
    return DocumentStore(tmp_path / "docs-root")


class _FakeEmbedder(ChunkEmbedder):
    """Deterministic offline embedder: one distinct unit vector per text."""

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.requests: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._fail:
            raise IndexingError(
                IndexErrorCode.EMBEDDING_UNAVAILABLE, "fixture endpoint down"
            )
        self.requests.append(list(texts))
        vectors = []
        for index, _ in enumerate(texts):
            vector = [0.0] * EMBEDDING_DIMENSIONS
            vector[index % EMBEDDING_DIMENSIONS] = 1.0
            vectors.append(vector)
        return vectors


def _indexer(sessions, store: DocumentStore, embedder: ChunkEmbedder) -> ScopeIndexer:
    return ScopeIndexer(sessions, store, embedder)


async def _fetchall(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).all()


async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _exec(sessions, sql: str, **params: object) -> None:
    async with sessions() as session:
        await session.execute(text(sql), params)
        await session.commit()


def _run(coro):
    """Run one coroutine on a throwaway loop (sync test helpers)."""
    return asyncio.run(coro)


async def _fts_count(sessions, term: str) -> int:
    row = await _fetchone(
        sessions,
        "SELECT count(*) AS n FROM knowledge_doc_chunks "
        "WHERE search_vector @@ plainto_tsquery('simple', :term)",
        term=term,
    )
    return int(row.n)


def _write_doc(
    store: DocumentStore,
    doc_id: str,
    text: str,
    *,
    revision: int = 0,
    server_id: str | None = None,
):
    return store.write(
        uuid.UUID(doc_id),
        text,
        server_id=uuid.UUID(server_id) if server_id else None,
        expected_revision=revision,
        origin="test-fixture",
    )


def _write_intent_file(
    store: DocumentStore,
    *,
    doc_id: str,
    op: str,
    revision: int,
    sha256: str | None,
    server_id: str | None = None,
    namespace: str = "docs",
) -> None:
    """Craft one durable intent directly (format pinned by 07-doc-store)."""
    if server_id is None:
        directory = store.root / "shared" / namespace / "reindex"
    else:
        directory = store.root / "servers" / server_id / namespace / "reindex"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "format": 1,
        "doc_id": doc_id,
        "server_id": server_id,
        "app_id": None,
        "namespace": namespace,
        "op": op,
        "revision": revision,
        "sha256": sha256,
        "tombstone": op == "delete",
    }
    (directory / f"{doc_id}.json").write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Acceptance 1 (chunk half) — section-aware, stable revision/offset chunks
# ---------------------------------------------------------------------------


class TestChunking:
    def test_sections_become_chunks_in_document_order(self) -> None:
        chunks = chunk_markdown(
            "# Title\n\nintro paragraph.\n\n## Alpha\n\nalpha body.\n\n"
            "## Beta\n\nbeta body.\n"
        )
        assert [chunk.ord for chunk in chunks] == [0, 1, 2]
        assert chunks[0].content.startswith("# Title")
        assert "intro paragraph." in chunks[0].content
        assert chunks[1].content.startswith("## Alpha")
        assert "alpha body." in chunks[1].content
        assert chunks[2].content.startswith("## Beta")

    def test_chunk_list_is_stable_for_same_revision_text(self) -> None:
        text = "# A\n\none\n\n## B\n\ntwo\n\n```python\ncode\n```\n"
        first = chunk_markdown(text)
        second = chunk_markdown(text)
        assert first == second  # retry/restart re-derives identical identity
        assert [chunk.ord for chunk in first] == list(range(len(first)))

    def test_document_without_headings_is_one_chunk(self) -> None:
        chunks = chunk_markdown("just a paragraph.\n\nand another.\n")
        assert len(chunks) == 1
        assert chunks[0].ord == 0

    def test_preamble_before_first_heading_keeps_its_own_section(self) -> None:
        chunks = chunk_markdown("preamble text.\n\n# First\n\nbody.\n")
        assert len(chunks) == 2
        assert chunks[0].content == "preamble text."
        assert chunks[1].content == "# First\n\nbody."

    def test_heading_only_section_yields_no_empty_chunk(self) -> None:
        chunks = chunk_markdown("# Empty\n\n# Real\n\ncontent here.\n")
        assert len(chunks) == 1
        assert chunks[0].ord == 0
        assert "content here." in chunks[0].content

    def test_oversized_section_splits_within_cap_at_block_boundaries(self) -> None:
        paragraphs = [f"paragraph {index} " + "x" * 300 for index in range(40)]
        text = "## Big\n\n" + "\n\n".join(paragraphs) + "\n"
        assert len(text) > MAX_CHUNK_CHARS
        chunks = chunk_markdown(text)
        assert len(chunks) > 1
        assert all(len(chunk.content) <= MAX_CHUNK_CHARS for chunk in chunks)
        assert all(chunk.content.startswith("## Big") for chunk in chunks)
        joined = "\n\n".join(chunk.content.split("\n\n", 1)[1] for chunk in chunks)
        for index, paragraph in enumerate(paragraphs):
            assert paragraph in joined, f"paragraph {index} lost or reordered"
        assert [chunk.ord for chunk in chunks] == list(range(len(chunks)))

    def test_single_oversized_line_is_hard_split(self) -> None:
        text = "# T\n\n" + "y" * (MAX_CHUNK_CHARS * 2 + 5) + "\n"
        chunks = chunk_markdown(text)
        assert len(chunks) >= 3
        assert all(len(chunk.content) <= MAX_CHUNK_CHARS for chunk in chunks)
        body = "".join(chunk.content.removeprefix("# T\n\n") for chunk in chunks)
        assert "y" * (MAX_CHUNK_CHARS * 2 + 5) in body

    @pytest.mark.parametrize(
        "info", ["env", "ENV", "dotenv", "pem", "p12", "key", "private-key"]
    )
    def test_credential_labelled_fence_is_excluded_structurally(self, info: str) -> None:
        # The deny decision keys on the declared info string only — the
        # block content here is deliberately benign, proving no content
        # scanning in either direction (C03 rule 6 posture).
        text = f"# S\n\nbefore.\n\n```{info}\nNOT_A_SECRET=1\n```\n\nafter.\n"
        chunks = chunk_markdown(text)
        rendered = "\n".join(chunk.content for chunk in chunks)
        assert "NOT_A_SECRET=1" not in rendered
        assert CREDENTIAL_FENCE_PLACEHOLDER in rendered
        assert "before." in rendered and "after." in rendered

    @pytest.mark.parametrize("info", ["python", "bash", "json", ""])
    def test_non_credential_fences_are_kept(self, info: str) -> None:
        text = f"# S\n\n```{info}\nkeep_me()\n```\n"
        rendered = "\n".join(chunk.content for chunk in chunk_markdown(text))
        assert "keep_me()" in rendered
        assert CREDENTIAL_FENCE_PLACEHOLDER not in rendered

    def test_unterminated_fence_is_one_block(self) -> None:
        chunks = chunk_markdown("# T\n\n```python\nnever closed\n")
        assert len(chunks) == 1
        assert "never closed" in chunks[0].content


# ---------------------------------------------------------------------------
# Acceptance 1 (embedder half) — pinned gateway surface, model identity
# ---------------------------------------------------------------------------


def _connection(**overrides: object):
    from mai.agents.models import EndpointCapabilities, ModelConnection

    fields: dict[str, object] = {
        "schema_version": 1,
        "name": "fixture-endpoint",
        "base_url": "http://127.0.0.1:9991",
        "credential_ref": None,
        "timeout_seconds": 5.0,
        "headers": (),
        "capabilities": EndpointCapabilities(),
    }
    fields.update(overrides)
    return ModelConnection(**fields)  # type: ignore[arg-type]


class TestGatewayEmbedder:
    """Offline embedder boundary: the mock transport stands in for the gateway.

    The explicit ``transport`` seam injects an httpx2 ``MockTransport`` (the
    same surface the pinned dependency test exercises); no endpoint is
    dialed and no production code path is patched.
    """

    def _embed(self, handler, *, connection=None, resolver=None,
               texts=("alpha text", "beta text")) -> list[list[float]]:
        import httpx2

        embedder = GatewayEmbedder(
            connection or _connection(),
            resolver=resolver,
            transport=httpx2.MockTransport(handler),
        )
        return _run(embedder.embed(list(texts)))

    def test_posts_to_v1_embeddings_with_pinned_model(self) -> None:
        import httpx2

        captured: dict[str, object] = {}

        async def handler(request: httpx2.Request) -> httpx2.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            vector = [0.5] * EMBEDDING_DIMENSIONS
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"embedding": vector, "index": 0},
                        {"embedding": vector, "index": 1},
                    ],
                    "usage": {"prompt_tokens": 2, "total_tokens": 2},
                },
            )

        vectors = self._embed(handler)
        assert captured["method"] == "POST"
        assert str(captured["url"]).endswith("/v1/embeddings")
        assert "/chat/completions" not in str(captured["url"])
        body = captured["body"]
        assert body["model"] == EMBEDDING_MODEL
        assert body["input"] == ["alpha text", "beta text"]
        assert [len(vector) for vector in vectors] == [
            EMBEDDING_DIMENSIONS,
            EMBEDDING_DIMENSIONS,
        ]

    def test_base_url_without_v1_suffix_gets_v1_appended(self) -> None:
        embedder = GatewayEmbedder(_connection(base_url="http://gw:9"), resolver=None)
        assert embedder._base_url() == "http://gw:9/v1"

    def test_base_url_already_v1_is_not_doubled(self) -> None:
        embedder = GatewayEmbedder(
            _connection(base_url="http://gw:9/v1"), resolver=None
        )
        assert embedder._base_url() == "http://gw:9/v1"

    def test_wrong_dimension_response_is_refused(self) -> None:
        import httpx2

        async def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.1] * (EMBEDDING_DIMENSIONS - 1)}
                    ]
                },
            )

        with pytest.raises(IndexingError) as err:
            self._embed(handler, texts=("only",))
        assert err.value.code is IndexErrorCode.DIMENSION_MISMATCH

    def test_broken_endpoint_is_unavailable_not_silent(self) -> None:
        import httpx2

        async def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(503, json={"error": "down"})

        with pytest.raises(IndexingError) as err:
            self._embed(handler, texts=("x",))
        assert err.value.code is IndexErrorCode.EMBEDDING_UNAVAILABLE

    def test_batches_large_inputs_preserving_order(self) -> None:
        import httpx2

        seen: list[list[str]] = []

        async def handler(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content)
            seen.append(list(body["input"]))
            offset = sum(len(batch) for batch in seen[:-1])
            return httpx2.Response(
                200,
                json={
                    "data": [
                        {
                            "embedding": [float(offset + i)]
                            + [0.0] * (EMBEDDING_DIMENSIONS - 1)
                        }
                        for i, _ in enumerate(body["input"])
                    ]
                },
            )

        texts = [f"text {i}" for i in range(EMBEDDING_BATCH_SIZE + 2)]
        vectors = self._embed(handler, texts=texts)
        assert [len(batch) for batch in seen] == [EMBEDDING_BATCH_SIZE, 2]
        assert [vector[0] for vector in vectors] == [
            float(i) for i in range(len(texts))
        ]

    def test_credential_ref_resolves_at_request_time_into_header(self, tmp_path):
        import httpx2
        from mai.credentials.env import EnvResolver

        env_file = tmp_path / "fixture.env"
        env_file.write_text("EMBEDDING_TOKEN=fxt-value\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        captured: dict[str, object] = {}

        async def handler(request: httpx2.Request) -> httpx2.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx2.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.0] * EMBEDDING_DIMENSIONS},
                        {"embedding": [0.0] * EMBEDDING_DIMENSIONS},
                    ]
                },
            )

        self._embed(
            handler,
            connection=_connection(credential_ref="EMBEDDING_TOKEN"),
            resolver=EnvResolver(env_file),
        )
        assert captured["auth"] == "Bearer fxt-value"

    def test_unresolvable_credential_ref_is_unavailable(self, tmp_path):
        from mai.credentials.env import EnvResolver

        env_file = tmp_path / "fixture.env"
        env_file.write_text("OTHER=1\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        embedder = GatewayEmbedder(
            _connection(credential_ref="EMBEDDING_TOKEN"),
            resolver=EnvResolver(env_file),
        )
        with pytest.raises(IndexingError) as err:
            _run(embedder.embed(["x"]))
        assert err.value.code is IndexErrorCode.EMBEDDING_UNAVAILABLE

    def test_ref_without_resolver_is_unavailable(self) -> None:
        embedder = GatewayEmbedder(
            _connection(credential_ref="EMBEDDING_TOKEN"), resolver=None
        )
        with pytest.raises(IndexingError) as err:
            _run(embedder.embed(["x"]))
        assert err.value.code is IndexErrorCode.EMBEDDING_UNAVAILABLE


# ---------------------------------------------------------------------------
# Acceptance 1 + 2 (storage half) — real DB reconcile, idempotency, staleness
# ---------------------------------------------------------------------------


def _outcome_for(outcomes, doc_id: str):
    matches = [o for o in outcomes if str(o.doc_id) == doc_id]
    assert len(matches) == 1, f"expected exactly one outcome for {doc_id}: {outcomes}"
    return matches[0]


class TestReconcileUpsert:
    async def test_upsert_indexes_chunks_with_dimension_tuple_and_fts(
        self, sessions, store
    ) -> None:
        doc = _write_doc(store, DOC_SHARED, "# Runbook\n\nrestart the service now.\n")
        outcomes = await _indexer(sessions, store, _FakeEmbedder()).reconcile_scope()
        outcome = _outcome_for(outcomes, DOC_SHARED)
        assert outcome.status is OutcomeStatus.INDEXED
        assert outcome.revision == 1
        assert outcome.chunks == 1

        row = await _fetchone(
            sessions,
            "SELECT revision, sha256, source_kind, origin, source_path, "
            "tombstone FROM knowledge_documents WHERE doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        assert row.revision == 1
        assert row.sha256 == doc.sha256
        assert row.source_kind == "authored"
        assert row.origin == "test-fixture"
        assert row.tombstone is False
        on_disk = store.root / "shared" / "docs" / f"{DOC_SHARED}.md"
        assert on_disk.is_file()
        assert row.source_path == str(on_disk.relative_to(store.root))

        chunks = await _fetchall(
            sessions,
            "SELECT ord, content, vector_dims(embedding) AS dims, "
            "embedding_provider, embedding_model, embedding_dimensions, "
            "distance_metric FROM knowledge_doc_chunks c "
            "JOIN knowledge_documents d ON d.document_key = c.document_key "
            "WHERE d.doc_id = :doc_id ORDER BY ord",
            doc_id=DOC_SHARED,
        )
        assert len(chunks) == 1
        assert chunks[0].ord == 0
        assert chunks[0].dims == EMBEDDING_DIMENSIONS
        assert (
            chunks[0].embedding_provider,
            chunks[0].embedding_model,
            chunks[0].embedding_dimensions,
            chunks[0].distance_metric,
        ) == DIMENSION_CONTRACT
        assert "restart the service" in chunks[0].content

        match = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM knowledge_doc_chunks c "
            "JOIN knowledge_documents d ON d.document_key = c.document_key "
            "WHERE d.doc_id = :doc_id "
            "AND c.search_vector @@ plainto_tsquery('simple', 'restart')",
            doc_id=DOC_SHARED,
        )
        assert match.n == 1
        assert store.read_intents() == []  # intent acknowledged

    async def test_confirmed_retry_is_unchanged_with_no_duplicate_chunks(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# A\n\nfirst revision body.\n")
        indexer = _indexer(sessions, store, _FakeEmbedder())
        first = await indexer.reconcile_scope()
        assert _outcome_for(first, DOC_SHARED).status is OutcomeStatus.INDEXED
        # The durable intent was purged on success; re-emitting the identical
        # intent (a duplicate delivery / resume) must not duplicate chunks.
        doc = store.read(uuid.UUID(DOC_SHARED))
        _write_intent_file(
            store,
            doc_id=DOC_SHARED,
            op="upsert",
            revision=doc.revision,
            sha256=doc.sha256,
        )
        second = await indexer.reconcile_scope()
        outcome = _outcome_for(second, DOC_SHARED)
        assert outcome.status is OutcomeStatus.UNCHANGED
        row = await _fetchone(
            sessions,
            "SELECT count(*) AS docs FROM knowledge_documents "
            "WHERE doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        chunks = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM knowledge_doc_chunks c "
            "JOIN knowledge_documents d ON d.document_key = c.document_key "
            "WHERE d.doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        assert row.docs == 1
        assert chunks.n == 1
        assert store.read_intents() == []

    async def test_crash_between_commit_and_purge_reconverges(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# A\n\nkill window body.\n")
        calls = {"purge": 0}
        real_purge = store.purge_intent

        def flaky_purge(doc_id, **kwargs) -> None:
            calls["purge"] += 1
            if calls["purge"] == 1:
                raise RuntimeError("simulated crash inside the ack window")
            real_purge(doc_id, **kwargs)

        store.purge_intent = flaky_purge  # type: ignore[method-assign]
        indexer = _indexer(sessions, store, _FakeEmbedder())
        with pytest.raises(RuntimeError, match="simulated crash"):
            await indexer.reconcile_scope()
        # Rows landed (the reconcile committed) but the intent is still
        # live: a visible pending index, not an invented sync.
        assert store.read_intents() != []
        # Restart: the same reconcile converges without duplicate chunks.
        store.purge_intent = real_purge  # type: ignore[method-assign]
        outcomes = await indexer.reconcile_scope()
        assert _outcome_for(outcomes, DOC_SHARED).status is OutcomeStatus.UNCHANGED
        assert store.read_intents() == []
        chunks = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM knowledge_doc_chunks c "
            "JOIN knowledge_documents d ON d.document_key = c.document_key "
            "WHERE d.doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        assert chunks.n == 1

    async def test_update_swaps_revision_and_removes_stale_results(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# Run\n\nalpha procedure step.\n")
        indexer = _indexer(sessions, store, _FakeEmbedder())
        await indexer.reconcile_scope()
        assert await _fts_count(sessions, "alpha") == 1

        _write_doc(
            store, DOC_SHARED, "# Run\n\nbeta procedure step.\n", revision=1
        )
        outcomes = await indexer.reconcile_scope()
        outcome = _outcome_for(outcomes, DOC_SHARED)
        assert outcome.status is OutcomeStatus.INDEXED
        assert outcome.revision == 2

        row = await _fetchone(
            sessions,
            "SELECT revision, count(*) OVER () AS live_docs FROM "
            "knowledge_documents WHERE doc_id = :doc_id AND NOT tombstone",
            doc_id=DOC_SHARED,
        )
        assert row.revision == 2
        assert row.live_docs == 1
        # Stale retrieval results are gone at the storage boundary.
        assert await _fts_count(sessions, "alpha") == 0
        assert await _fts_count(sessions, "beta") == 1

    async def test_model_identity_tuple_is_the_frozen_v1_on_every_row(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# Doc\n\none\n\ntwo\n\nthree\n")
        await _indexer(sessions, store, _FakeEmbedder()).reconcile_scope()
        rows = await _fetchall(
            sessions,
            "SELECT DISTINCT embedding_provider, embedding_model, "
            "embedding_dimensions, distance_metric FROM knowledge_doc_chunks"
        )
        assert len(rows) == 1
        assert (
            rows[0].embedding_provider,
            rows[0].embedding_model,
            rows[0].embedding_dimensions,
            rows[0].distance_metric,
        ) == DIMENSION_CONTRACT

    async def test_scope_isolation_server_app_shared_at_storage_boundary(
        self, sessions, store
    ) -> None:
        """KB-FILTER writer half: same doc identity, three scopes, no mixing."""
        for server in (SERVER_A, SERVER_B):
            _write_doc(
                store,
                DOC_A,
                f"# Server doc\n\nprocedure for {server}.\n",
                server_id=server,
            )
        _write_doc(store, DOC_A, "# Shared doc\n\nshared procedure.\n")
        indexer = _indexer(sessions, store, _FakeEmbedder())
        # The outbox is swept per scope; each sweep reconciles exactly its
        # own intents — cross-scope reads are unrepresentable by construction.
        outcomes = []
        outcomes += await indexer.reconcile_scope()
        outcomes += await indexer.reconcile_scope(server_id=uuid.UUID(SERVER_A))
        outcomes += await indexer.reconcile_scope(server_id=uuid.UUID(SERVER_B))
        assert len(outcomes) == 3
        assert {outcome.status for outcome in outcomes} == {
            OutcomeStatus.INDEXED
        }
        rows = await _fetchall(
            sessions,
            "SELECT server_id, count(*) AS n FROM knowledge_documents "
            "WHERE doc_id = :doc_id GROUP BY server_id ORDER BY server_id NULLS LAST",
            doc_id=DOC_A,
        )
        assert len(rows) == 3
        assert {row.server_id for row in rows} == {
            uuid.UUID(SERVER_A),
            uuid.UUID(SERVER_B),
            None,
        }
        scoped = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM knowledge_doc_chunks c "
            "JOIN knowledge_documents d ON d.document_key = c.document_key "
            "WHERE d.doc_id = :doc_id AND d.server_id = :server_id "
            "AND c.search_vector @@ plainto_tsquery('simple', 'shared')",
            doc_id=DOC_A,
            server_id=uuid.UUID(SERVER_A),
        )
        assert scoped.n == 0  # server A never sees the shared corpus


class TestReconcileDelete:
    async def test_delete_removes_rows_chunks_and_purges_intent(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# Gone\n\ndeletable body.\n")
        indexer = _indexer(sessions, store, _FakeEmbedder())
        await indexer.reconcile_scope()
        assert await _fts_count(sessions, "deletable") == 1
        doc = store.read(uuid.UUID(DOC_SHARED))
        store.delete(uuid.UUID(DOC_SHARED), expected_revision=doc.revision)

        outcomes = await indexer.reconcile_scope()
        outcome = _outcome_for(outcomes, DOC_SHARED)
        assert outcome.status is OutcomeStatus.DELETED
        rows = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM knowledge_documents WHERE doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        chunks = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM knowledge_doc_chunks c "
            "JOIN knowledge_documents d ON d.document_key = c.document_key "
            "WHERE d.doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        assert rows.n == 0
        assert chunks.n == 0
        assert await _fts_count(sessions, "deletable") == 0
        assert store.read_intents() == []

    async def test_delete_retry_after_ack_crash_is_absent(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# Gone\n\nbody.\n")
        indexer = _indexer(sessions, store, _FakeEmbedder())
        await indexer.reconcile_scope()
        doc = store.read(uuid.UUID(DOC_SHARED))
        store.delete(uuid.UUID(DOC_SHARED), expected_revision=doc.revision)

        calls = {"purge": 0}
        real_purge = store.purge_intent

        def flaky_purge(doc_id, **kwargs) -> None:
            calls["purge"] += 1
            if calls["purge"] == 1:
                raise RuntimeError("simulated crash after delete confirm")
            real_purge(doc_id, **kwargs)

        store.purge_intent = flaky_purge  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="simulated crash"):
            await indexer.reconcile_scope()
        assert store.read_intents() != []
        store.purge_intent = real_purge  # type: ignore[method-assign]
        outcomes = await indexer.reconcile_scope()
        assert _outcome_for(outcomes, DOC_SHARED).status is OutcomeStatus.ABSENT
        assert store.read_intents() == []

    async def test_delete_phase_one_crash_window_reconverges(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# Gone\n\nphase one body.\n")
        indexer = _indexer(sessions, store, _FakeEmbedder())
        await indexer.reconcile_scope()
        doc = store.read(uuid.UUID(DOC_SHARED))
        store.delete(uuid.UUID(DOC_SHARED), expected_revision=doc.revision)

        # Simulate the crash after phase 1 (tombstone + chunk removal
        # committed, row purge and intent ack not): the next reconcile
        # must converge through phase 2 without duplicating anything.
        await _exec(
            sessions,
            "UPDATE knowledge_documents SET tombstone = true, "
            "tombstoned_at = now() WHERE doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        await _exec(
            sessions,
            "DELETE FROM knowledge_doc_chunks WHERE document_key IN "
            "(SELECT document_key FROM knowledge_documents WHERE doc_id = :doc_id)",
            doc_id=DOC_SHARED,
        )
        assert await _fts_count(sessions, "phase") == 0
        outcomes = await indexer.reconcile_scope()
        assert _outcome_for(outcomes, DOC_SHARED).status is OutcomeStatus.DELETED
        rows = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM knowledge_documents WHERE doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        assert rows.n == 0
        assert store.read_intents() == []

    async def test_delete_of_unknown_source_is_absent_and_purged(
        self, sessions, store
    ) -> None:
        _write_intent_file(
            store,
            doc_id=DOC_SHARED,
            op="delete",
            revision=3,
            sha256=None,
        )
        outcomes = await _indexer(sessions, store, _FakeEmbedder()).reconcile_scope()
        outcome = _outcome_for(outcomes, DOC_SHARED)
        assert outcome.status is OutcomeStatus.ABSENT
        assert store.read_intents() == []


class TestReconcilePending:
    async def test_unavailable_embedding_leaves_pending_and_no_empty_index(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# Wait\n\nembed me later.\n")
        outcomes = await _indexer(sessions, store, _FakeEmbedder(fail=True)).reconcile_scope()
        outcome = _outcome_for(outcomes, DOC_SHARED)
        assert outcome.status is OutcomeStatus.PENDING
        assert outcome.code is IndexErrorCode.EMBEDDING_UNAVAILABLE
        # The durable intent stays visible (pending index, not empty-success)…
        assert [i.doc_id for i in store.read_intents()] == [uuid.UUID(DOC_SHARED)]
        # …and nothing was written to the projection.
        rows = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM knowledge_documents WHERE doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        assert rows.n == 0

    async def test_failed_reembed_keeps_previous_revision_retrievable(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# V1\n\nstable searchable body.\n")
        indexer = _indexer(sessions, store, _FakeEmbedder())
        await indexer.reconcile_scope()
        assert await _fts_count(sessions, "stable") == 1

        _write_doc(store, DOC_SHARED, "# V2\n\nnew body.\n", revision=1)
        failing = _indexer(sessions, store, _FakeEmbedder(fail=True))
        outcomes = await failing.reconcile_scope()
        outcome = _outcome_for(outcomes, DOC_SHARED)
        assert outcome.status is OutcomeStatus.PENDING
        # Never an empty index: the previous revision still answers search…
        assert await _fts_count(sessions, "stable") == 1
        # …and the new intent stays visible for the next reconcile.
        assert [i.doc_id for i in store.read_intents()] == [uuid.UUID(DOC_SHARED)]
        healed = await indexer.reconcile_scope()
        assert _outcome_for(healed, DOC_SHARED).status is OutcomeStatus.INDEXED
        assert await _fts_count(sessions, "stable") == 0
        assert await _fts_count(sessions, "new") == 1

    async def test_missing_source_leaves_pending_not_purged(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# Lost\n\nbody.\n")
        # Simulate a crash that removed the canonical pair while the upsert
        # intent is still live.
        for suffix in (".md", ".doc.meta.json"):
            (store.root / "shared" / "docs" / f"{DOC_SHARED}{suffix}").unlink()
        outcomes = await _indexer(sessions, store, _FakeEmbedder()).reconcile_scope()
        outcome = _outcome_for(outcomes, DOC_SHARED)
        assert outcome.status is OutcomeStatus.PENDING
        assert outcome.code is IndexErrorCode.SOURCE_MISSING
        assert [i.doc_id for i in store.read_intents()] == [uuid.UUID(DOC_SHARED)]

    async def test_hash_mismatch_leaves_pending_not_purged(
        self, sessions, store
    ) -> None:
        _write_doc(store, DOC_SHARED, "# T\n\nbody.\n")
        doc = store.read(uuid.UUID(DOC_SHARED))
        tampered = hashlib.sha256(b"other content").hexdigest()
        _write_intent_file(
            store,
            doc_id=DOC_SHARED,
            op="upsert",
            revision=doc.revision,
            sha256=tampered,
        )
        outcomes = await _indexer(sessions, store, _FakeEmbedder()).reconcile_scope()
        outcome = _outcome_for(outcomes, DOC_SHARED)
        assert outcome.status is OutcomeStatus.PENDING
        assert outcome.code is IndexErrorCode.HASH_MISMATCH
        assert [i.doc_id for i in store.read_intents()] == [uuid.UUID(DOC_SHARED)]
        rows = await _fetchone(
            sessions,
            "SELECT count(*) AS n FROM knowledge_documents WHERE doc_id = :doc_id",
            doc_id=DOC_SHARED,
        )
        assert rows.n == 0


# ---------------------------------------------------------------------------
# Production entrypoint — real OS process, real DB, real local HTTP fixture
# ---------------------------------------------------------------------------


class _EmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        vectors = []
        for _ in body["input"]:
            vector = [0.01] * EMBEDDING_DIMENSIONS
            vectors.append(vector)
        payload = json.dumps(
            {"data": [{"embedding": vector} for vector in vectors]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


@pytest.mark.skipif(
    not os.environ.get(LIVE_URL_VAR), reason=f"{LIVE_URL_VAR} not set"
)
class TestCliEntrypoint:
    def test_reconcile_subprocess_roundtrip(self, migrated_url, store, tmp_path) -> None:
        _write_doc(store, DOC_SHARED, "# CLI\n\nsubprocess indexed body.\n")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = dict(os.environ)
        env["MAI_DATABASE_URL"] = migrated_url
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mai.knowledge.index",
                    "reconcile",
                    str(store.root),
                    "--shared",
                    "--base-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--timeout-seconds",
                    "5",
                ],
                cwd=BACKEND_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert proc.returncode == 0, proc.stderr
        lines = [json.loads(line) for line in proc.stdout.splitlines()]
        intent = [line for line in lines if line.get("kind") == "intent"]
        summary = [line for line in lines if line.get("kind") == "summary"]
        assert len(intent) == 1 and len(summary) == 1
        assert intent[0]["status"] == "indexed"
        assert intent[0]["chunks"] == 1
        assert intent[0]["code"] is None
        assert summary[0] == {"kind": "summary", "intents": 1, "pending": 0}
        # Value-free output: the document text never leaks to stdout.
        assert "subprocess indexed body" not in proc.stdout
        assert store.read_intents() == []
