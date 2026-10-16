"""Markdown section-aware chunking and idempotent pgvector/FTS reindexing.

Card ``07-chunk-index`` (service ``knowledge``), against contracts.md **C07**
rules 2/3 and the knowledge-packaging ADR (``5d6e0a1``) §4.2 dimension
contract / §4.3 rebuild contract, consuming the reviewed prerequisites
``07-doc-store`` (canonical file store + durable reindex intent outbox),
``07-vector-schema`` (``knowledge_documents`` / ``knowledge_doc_chunks``
projection schema), ``05-steps`` (durable-step retry discipline: every
intent reconciliation is idempotent and crash-safe, so a killed indexer
restarted by the engine converges without duplicate current-source chunks)
and ``06-model-config``/``03-credentials`` (validated OpenAI-compatible
endpoint connection; credentials resolve by reference at the point of use).

- **C07 rule 2** — chunk IDs derive from source revision/offset: the chunk
  primary key ``(document_key, ord)`` is revision-specific because the parent
  ``knowledge_documents`` row is revision-specific; a source update swaps the
  parent row (cascade-deleting stale chunks) and a delete removes it. The
  server/shared scope filter is applied at this storage boundary by the
  scope-keyed parent row — never prompt-only.
- **ADR §4.2 (dimension contract, binding)** — the only active embedding
  identity is the frozen tuple ``("openai-compatible",
  "text-embedding-3-small", 1536, "cosine")``: the embedder always requests
  the pinned model, asserts every returned vector has the pinned dimension
  (the ingest-path half of the write-time check; the ``vector(1536)`` column
  type and the CHECK constraints are the other half), and the reconciler
  writes the tuple columns from the same constants the migration pins. A
  gateway/connection change can therefore never silently mix models or dims
  into one index — it is a migration/rebuild event (rule 3 refusal is
  structural here: the embedder refuses to hand a wrong-length vector to the
  write path, so the intent stays pending instead of corrupting the index).
- **ADR §4.3 (rebuild contract)** — all vector data is derived: the
  reconciler rebuilds a source as delete → re-chunk → re-embed → re-index,
  embedding *before* swapping rows so an unavailable embedding endpoint
  leaves the previous index (or no index) plus the durable intent — a
  visible pending index, never an empty-success.
- **C03 rule 6 posture (credentials)** — the chunker never scans content for
  secret material; its exclusion policy is structural exactly like the
  doc-store import deny-list: fenced code blocks whose *declared info
  string* names a credential format are replaced by a fixed placeholder
  before chunking, so credential material never reaches the embedding
  request or the index.
- **Outbox lifecycle (C02 W4)** — an intent is purged only after the
  projection confirms the reconcile (``DocumentStore.purge_intent`` is the
  indexer ack). Deletes are two-phase: tombstone the projection rows and
  remove their chunks first (durable, retrieval results gone), then purge
  the rows and the intent after confirmation; any crash window re-converges
  on restart because both phases are idempotent against intent + row state.

The module reads no process environment and no clock except the
projection-owned ``tombstoned_at`` timestamp; the network seam is the
embedder alone. Chunking is pure stdlib (no Markdown dependency: structural
line grammar only — headings, fenced blocks, blank-line paragraphs).

Production entrypoint (exit contract 0 reconcile ran — per-intent statuses
are in the JSON output, ``pending`` included; 1 structured rejection; 2
usage or IO error)::

    uv run python -m mai.knowledge.index reconcile ROOT (--server ID | --shared) [--app ID] \
        --base-url URL [--credential-ref NAME] [--env-file PATH] [--timeout-seconds N]

The database URL comes from ``MAI_DATABASE_URL`` (``mai.database``); the
embedding credential reference resolves through the ``03-credentials``
``.env`` resolver at request time — secret material never appears in output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mai.agents.models import (
    SCHEMA_VERSION as CONNECTION_SCHEMA_VERSION,
    ModelConnection,
    parse_connection,
)
from mai.credentials.env import EnvResolver
from mai.knowledge.documents import (
    DocumentNamespace,
    DocumentStore,
    DocumentStoreError,
    ReindexIntent,
)

__all__ = [
    "CREDENTIAL_FENCE_PLACEHOLDER",
    "DISTANCE_METRIC",
    "EMBEDDING_BATCH_SIZE",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL",
    "EMBEDDING_PROVIDER",
    "Chunk",
    "ChunkEmbedder",
    "GatewayEmbedder",
    "IndexErrorCode",
    "IndexingError",
    "MAX_CHUNK_CHARS",
    "ReconcileOutcome",
    "ScopeIndexer",
    "chunk_markdown",
]


# ---------------------------------------------------------------------------
# Frozen dimension contract (knowledge-packaging ADR §4.2; contracts.md C07
# rule 3; mirrored from backend/migrations/versions/0005_knowledge.py and
# backend/tests/test_knowledge_dependencies.py — a drift between any of the
# pins fails loudly at the CHECK-constraint, dependency or test layer).
# ---------------------------------------------------------------------------

EMBEDDING_PROVIDER = "openai-compatible"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
DISTANCE_METRIC = "cosine"

# Chunking parameters (ADR §6: chunking parameters are this card's contract;
# frozen here, deterministic across retries). A chunk never exceeds the hard
# character cap; oversized sections are split at paragraph (block) boundaries
# so chunk offsets stay stable for unchanged prefix content.
MAX_CHUNK_CHARS = 4000

# Embeddings are requested in bounded batches so one document reconcile
# never builds an unbounded request; batch order preserves chunk order.
EMBEDDING_BATCH_SIZE = 128

# Structural credential exclusion (C03 rule 6: key on the declared fence
# info string, never on content): fenced blocks labelled with one of these
# info strings are replaced by the placeholder before chunking.
CREDENTIAL_FENCE_INFOS = frozenset(
    {
        "env",
        "dotenv",
        "pem",
        "p12",
        "pfx",
        "key",
        "private-key",
        "private_key",
        "rsa",
        "dsa",
        "ec",
        "ssh",
        "openssh",
        "putty",
        "pgp",
        "gpg",
    }
)
CREDENTIAL_FENCE_PLACEHOLDER = "[credential material excluded before chunking]"

_HEADING_MARK = "#"
_FENCE_CHARS = ("`", "~")


# ---------------------------------------------------------------------------
# Errors (C01 envelope vocabulary style, value-free details)
# ---------------------------------------------------------------------------


class IndexErrorCode(StrEnum):
    """Stable machine-readable reconcile rejection codes."""

    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    DIMENSION_MISMATCH = "dimension_mismatch"
    SOURCE_MISSING = "source_missing"
    HASH_MISMATCH = "hash_mismatch"
    INVALID_INTENT = "invalid_intent"


class IndexingError(ValueError):
    """One structured, value-free indexing rejection."""

    def __init__(self, code: IndexErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Section-aware Markdown chunking (pure, deterministic, clock-free)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chunk:
    """One chunk of a source revision: ordinal offset plus rendered text.

    The ordinal is the chunk's position within the revision's chunk list —
    together with the revision-specific parent row this is the C07 rule-2
    chunk identity (source revision/offset). Stable across retries: the same
    revision content always yields the same chunk list.
    """

    ord: int
    content: str


def _fence_opener(line: str) -> tuple[str, str] | None:
    """Return (fence_char, info_string) for an ATX-style fence opener."""
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    if indent > 3 or len(stripped) < 3:
        return None
    char = stripped[0]
    if char not in _FENCE_CHARS:
        return None
    run = 0
    while run < len(stripped) and stripped[run] == char:
        run += 1
    if run < 3:
        return None
    info = stripped[run:].strip()
    if char == "`" and "`" in info:
        return None  # backtick fence info string may not contain backticks
    return (char * run, info)


def _is_fence_closer(line: str, opener: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and set(stripped) == {opener[0]} and len(stripped) >= len(opener)


def _is_heading(line: str) -> bool:
    stripped = line.lstrip()
    if len(line) - len(stripped) > 3:
        return False
    level = 0
    while level < len(stripped) and stripped[level] == _HEADING_MARK:
        level += 1
    if level < 1 or level > 6:
        return False
    return len(stripped) > level and stripped[level] in (" ", "\t")


def _blocks(lines: list[str]) -> list[str]:
    """Group lines into structural blocks: fenced code, headings, paragraphs.

    Credential-labelled fences are replaced by the placeholder block here —
    the exclusion is structural (declared info string), never content-based.
    Trailing blank lines never form a block.
    """
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        opener = _fence_opener(line)
        if opener is not None:
            fence, info = opener
            body: list[str] = [line]
            i += 1
            closed = False
            while i < len(lines):
                body.append(lines[i])
                if _is_fence_closer(lines[i], fence):
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                # Unterminated fence: the rest of the document is the block.
                pass
            if info.lower() in CREDENTIAL_FENCE_INFOS:
                blocks.append(CREDENTIAL_FENCE_PLACEHOLDER)
            else:
                blocks.append("\n".join(body))
            continue
        if _is_heading(line):
            blocks.append(line)
            i += 1
            continue
        paragraph = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and _fence_opener(lines[i]) is None and not _is_heading(lines[i]):
            paragraph.append(lines[i])
            i += 1
        blocks.append("\n".join(paragraph))
    return blocks


def _hard_split(block: str, limit: int) -> list[str]:
    """Split one oversized block at line, then character, boundaries."""
    if len(block) <= limit:
        return [block]
    parts: list[str] = []
    current = ""
    for line in block.split("\n"):
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        parts.append(current)
    return [part for part in parts if part]


def _split_section(heading: str | None, content: list[str]) -> list[str]:
    """Render one section into <= MAX_CHUNK_CHARS chunk texts.

    ``content`` are the section's non-heading blocks in order. Greedy
    block packing keeps boundaries at paragraph granularity; a single
    oversized block falls back to a line/character hard split. The heading
    prefix counts against the cap so no rendered chunk exceeds it.
    """
    prefix = f"{heading}\n\n" if heading else ""
    if len(prefix) >= MAX_CHUNK_CHARS:
        # Pathological overlong heading: the cap wins over the prefix so
        # the rendered-chunk invariant always holds.
        prefix = ""
    budget = MAX_CHUNK_CHARS - len(prefix)
    texts: list[str] = []

    def flush(packed: list[str]) -> None:
        if not packed:
            return
        body = "\n\n".join(packed)
        texts.append(prefix + body if prefix else body)

    packed: list[str] = []
    packed_len = 0
    for block in content:
        candidates = _hard_split(block, budget) if len(block) > budget else [block]
        for piece in candidates:
            addition = len(piece) if not packed else len(piece) + 2
            if packed and packed_len + addition > budget:
                flush(packed)
                packed = []
                packed_len = 0
            packed.append(piece)
            packed_len += addition
    flush(packed)
    return texts


def chunk_markdown(text: str) -> list[Chunk]:
    """Chunk one canonical Markdown revision, section-aware and stable.

    Every ATX heading starts a new section (flat grammar — level-independent,
    predictable); content before the first heading is the preamble section.
    A heading-only section yields no chunk (the chunk content CHECK forbids
    empty text). The same revision text always yields the same chunk list,
    so retried or resumed reconciles re-derive identical (revision, offset)
    identities.
    """
    normalized = text.lstrip("\ufeff")
    lines = normalized.splitlines()
    sections: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    content: list[str] = []
    for block in _blocks(lines):
        if _is_heading(block):
            sections.append((heading, content))
            heading = block
            content = []
        else:
            content.append(block)
    sections.append((heading, content))

    chunks: list[Chunk] = []
    for section_heading, section_content in sections:
        for rendered in _split_section(section_heading, section_content):
            if rendered.strip():
                chunks.append(Chunk(ord=len(chunks), content=rendered))
    return chunks


# ---------------------------------------------------------------------------
# Embedding seam (model config / gateway consumer)
# ---------------------------------------------------------------------------


class ChunkEmbedder:
    """Async embedding seam: deterministic chunk order in, vectors out.

    The production implementation is :class:`GatewayEmbedder`; tests inject
    fakes. Any failure raises :class:`IndexingError` with
    :attr:`IndexErrorCode.EMBEDDING_UNAVAILABLE` (or ``DIMENSION_MISMATCH``)
    so the reconciler leaves the intent pending instead of writing
    empty-success index state.
    """

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError


class GatewayEmbedder(ChunkEmbedder):
    """Embed chunk texts through the validated OpenAI-compatible connection.

    ``06-model-config`` owns the connection document; ``03-credentials``
    owns resolution: ``credential_ref`` resolves through the supplied
    :class:`EnvResolver` at request time (the point of use), and the
    resolved material is passed only into the SDK client constructor —
    never logged, never included in outcomes or errors. The request targets
    ``{base_url}/v1/embeddings`` (C07 rule 7: the embeddings API, never
    ``/chat/completions``); the pinned model and dimension contract are this
    module's frozen constants, not connection settings, so one index can
    never mix models or dimensions (ADR §4.2/§4.3).
    """

    def __init__(
        self,
        connection: ModelConnection,
        resolver: EnvResolver | None = None,
        *,
        transport: object | None = None,
    ) -> None:
        self._connection = connection
        self._resolver = resolver
        # Test seam: an explicit httpx2 transport (e.g. MockTransport) keeps
        # the gateway boundary provable offline; production passes None and
        # the client dials the configured endpoint.
        self._transport = transport

    def _api_key(self) -> str:
        ref = self._connection.credential_ref
        if ref is None:
            # Unauthenticated endpoint: the SDK requires a placeholder key;
            # it is sent only as an Authorization header value to the
            # configured endpoint, which declares itself credential-free.
            return "mai-unauthenticated-endpoint"
        if self._resolver is None:
            raise IndexingError(
                IndexErrorCode.EMBEDDING_UNAVAILABLE,
                "connection declares a credential reference but no .env "
                "resolver is configured",
            )
        try:
            return self._resolver.resolve(ref).value
        except Exception as exc:  # noqa: BLE001 - classified, value-free
            raise IndexingError(
                IndexErrorCode.EMBEDDING_UNAVAILABLE,
                f"credential reference did not resolve: {type(exc).__name__}",
            ) from None

    def _base_url(self) -> str:
        base = self._connection.base_url
        return base if base.rstrip("/").endswith("/v1") else f"{base.rstrip('/')}/v1"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        import httpx2
        import openai

        headers = dict(self._connection.headers)
        timeout = self._connection.timeout_seconds
        client = openai.AsyncOpenAI(
            base_url=self._base_url(),
            api_key=self._api_key(),
            default_headers=headers or None,
            http_client=httpx2.AsyncClient(timeout=timeout, transport=self._transport),
        )
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
                batch = list(texts[start : start + EMBEDDING_BATCH_SIZE])
                try:
                    response = await client.embeddings.create(
                        model=EMBEDDING_MODEL, input=batch
                    )
                except IndexingError:
                    raise
                except Exception as exc:  # noqa: BLE001 - classified, value-free
                    raise IndexingError(
                        IndexErrorCode.EMBEDDING_UNAVAILABLE,
                        f"embedding endpoint did not serve a usable response: "
                        f"{type(exc).__name__}",
                    ) from None
                batch_vectors = [item.embedding for item in response.data]
                if len(batch_vectors) != len(batch):
                    raise IndexingError(
                        IndexErrorCode.EMBEDDING_UNAVAILABLE,
                        "embedding endpoint returned a different vector count "
                        "than the chunk count it was asked to embed",
                    )
                for vector in batch_vectors:
                    if len(vector) != EMBEDDING_DIMENSIONS:
                        raise IndexingError(
                            IndexErrorCode.DIMENSION_MISMATCH,
                            f"embedding response carried {len(vector)} "
                            f"dimensions, the index is pinned at "
                            f"{EMBEDDING_DIMENSIONS}; a model/dims switch is "
                            f"a rebuild event, never silently mixed",
                        )
                vectors.extend(batch_vectors)
        finally:
            await client.close()
        return vectors


# ---------------------------------------------------------------------------
# Reconcile outcomes
# ---------------------------------------------------------------------------


class OutcomeStatus(StrEnum):
    """Per-intent reconcile result (``pending`` = intent left visible)."""

    INDEXED = "indexed"
    UNCHANGED = "unchanged"
    DELETED = "deleted"
    ABSENT = "absent"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """Value-free per-intent reconcile record (never chunk/secret content)."""

    doc_id: uuid.UUID
    namespace: str
    op: str
    status: OutcomeStatus
    revision: int | None
    chunks: int
    code: IndexErrorCode | None = None


# ---------------------------------------------------------------------------
# Scope indexer: idempotent reindexing consumer for the doc-store outbox
# ---------------------------------------------------------------------------


_SCOPE_KEYS = (
    "server_id IS NOT DISTINCT FROM :server_id AND "
    "app_id IS NOT DISTINCT FROM :app_id AND "
    "namespace = :namespace AND doc_id = :doc_id"
)


def _source_path(
    server_id: uuid.UUID | None,
    app_id: uuid.UUID | None,
    namespace: DocumentNamespace,
    doc_id: uuid.UUID,
) -> str:
    """Canonical-store relative source path (mirrors ``documents._paths``).

    The layout authority is ``mai.knowledge.documents``; this mirror is kept
    in sync by a test that compares against the real on-disk path, and by
    the projection's ``source_path`` CHECK bounds.
    """
    collection = f"{namespace.value}/{doc_id}.md"
    if server_id is None:
        return f"shared/{collection}"
    if app_id is not None:
        return f"servers/{server_id}/applications/{app_id}/{collection}"
    return f"servers/{server_id}/{collection}"


class ScopeIndexer:
    """Idempotent consumer of one scope's durable reindex intent outbox.

    Reconciles ``DocumentStore.read_intents`` results into the
    ``07-vector-schema`` projection (``knowledge_documents`` +
    ``knowledge_doc_chunks``), then acknowledges with
    :meth:`DocumentStore.purge_intent` — after the projection confirms,
    never before (C02 W4). Every path is safe to re-run: a crash between
    any two commits leaves intent + row state that the next reconcile
    converges from (the ``05-steps`` durable retry discipline; a killed
    indexer restarted mid-batch never duplicates current-source chunks).
    """

    def __init__(
        self,
        sessions: async_sessionmaker,
        store: DocumentStore,
        embedder: ChunkEmbedder,
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._embedder = embedder

    async def reconcile_scope(
        self,
        *,
        server_id: uuid.UUID | None = None,
        app_id: uuid.UUID | None = None,
    ) -> list[ReconcileOutcome]:
        """Reconcile every live intent of one scope; per-intent isolation."""
        intents = self._store.read_intents(server_id=server_id, app_id=app_id)
        outcomes: list[ReconcileOutcome] = []
        for intent in intents:
            try:
                outcome = await self._reconcile_intent(intent)
            except IndexingError as exc:
                outcome = ReconcileOutcome(
                    doc_id=intent.doc_id,
                    namespace=intent.namespace.value,
                    op=intent.op,
                    status=OutcomeStatus.PENDING,
                    revision=intent.revision,
                    chunks=0,
                    code=exc.code,
                )
            outcomes.append(outcome)
        return outcomes

    # -- upsert ------------------------------------------------------------

    async def _reconcile_upsert(self, intent: ReindexIntent) -> ReconcileOutcome:
        doc = self._load_source(intent)
        if intent.sha256 is not None and intent.sha256 != doc.sha256:
            raise IndexingError(
                IndexErrorCode.HASH_MISMATCH,
                "reindex intent hash differs from the stored source revision; "
                "the canonical store must be reconciled before indexing",
            )
        chunks = chunk_markdown(doc.text)
        # Embed BEFORE touching any row: an unavailable embedding endpoint
        # then leaves the previous index (or none) plus the durable intent —
        # a visible pending index, never an empty-success swap.
        embeddings = await self._embedder.embed([chunk.content for chunk in chunks])
        if len(embeddings) != len(chunks):
            raise IndexingError(
                IndexErrorCode.EMBEDDING_UNAVAILABLE,
                "embedder returned a different vector count than the chunk "
                "count it was asked to embed",
            )

        scope = {
            "server_id": intent.server_id,
            "app_id": intent.app_id,
            "namespace": intent.namespace.value,
            "doc_id": str(intent.doc_id),
        }
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT document_key, revision, sha256, tombstone "
                        f"FROM knowledge_documents WHERE {_SCOPE_KEYS}"
                    ),
                    scope,
                )
            ).all()
            live = [row for row in rows if not row.tombstone]
            chunk_counts = {
                str(row.document_key): (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM knowledge_doc_chunks "
                            "WHERE document_key = :key"
                        ),
                        {"key": str(row.document_key)},
                    )
                ).scalar()
                for row in rows
            }
            if (
                len(live) == 1
                and len(rows) == 1
                and live[0].revision == doc.revision
                and live[0].sha256 == doc.sha256
                and chunk_counts[str(live[0].document_key)] == len(chunks)
            ):
                # Idempotent retry: the current revision is already fully
                # indexed; acknowledge only.
                pass
            else:
                # ADR §4.3 rebuild: delete (cascade removes stale chunks) →
                # re-chunk (done above) → re-embed (done above) → re-index.
                await session.execute(
                    text(f"DELETE FROM knowledge_documents WHERE {_SCOPE_KEYS}"),
                    scope,
                )
                document_key = (
                    await session.execute(
                        text(
                            "INSERT INTO knowledge_documents "
                            "(doc_id, server_id, app_id, namespace, revision, "
                            " sha256, source_kind, origin, origin_revision, "
                            " verification, source_path, tombstone, "
                            " tombstoned_at) "
                            "VALUES (:doc_id, :server_id, :app_id, :namespace, "
                            " :revision, :sha256, :source_kind, :origin, "
                            " :origin_revision, :verification, :source_path, "
                            " false, NULL) RETURNING document_key"
                        ),
                        {
                            **scope,
                            "revision": doc.revision,
                            "sha256": doc.sha256,
                            "source_kind": doc.source_kind.value,
                            "origin": doc.origin,
                            "origin_revision": doc.origin_revision,
                            "verification": doc.verification.value,
                            "source_path": _source_path(
                                intent.server_id,
                                intent.app_id,
                                intent.namespace,
                                intent.doc_id,
                            ),
                        },
                    )
                ).scalar()
                if chunks:
                    await session.execute(
                        text(
                            "INSERT INTO knowledge_doc_chunks "
                            "(document_key, ord, content, embedding, "
                            " embedding_provider, embedding_model, "
                            " embedding_dimensions, distance_metric) "
                            "VALUES (:document_key, :ord, :content, "
                            " CAST(:embedding AS vector), :provider, :model, "
                            " :dimensions, :metric)"
                        ),
                        [
                            {
                                "document_key": str(document_key),
                                "ord": chunk.ord,
                                "content": chunk.content,
                                "embedding": _vector_literal(embeddings[chunk.ord]),
                                "provider": EMBEDDING_PROVIDER,
                                "model": EMBEDDING_MODEL,
                                "dimensions": EMBEDDING_DIMENSIONS,
                                "metric": DISTANCE_METRIC,
                            }
                            for chunk in chunks
                        ],
                    )
                await session.commit()
        self._store.purge_intent(
            intent.doc_id,
            server_id=intent.server_id,
            app_id=intent.app_id,
            namespace=intent.namespace,
        )
        status = (
            OutcomeStatus.UNCHANGED
            if len(live) == 1 and len(rows) == 1 and live[0].revision == doc.revision
            else OutcomeStatus.INDEXED
        )
        return ReconcileOutcome(
            doc_id=intent.doc_id,
            namespace=intent.namespace.value,
            op=intent.op,
            status=status,
            revision=doc.revision,
            chunks=len(chunks),
        )

    def _load_source(self, intent: ReindexIntent):
        try:
            return self._store.read(
                intent.doc_id,
                server_id=intent.server_id,
                app_id=intent.app_id,
                namespace=intent.namespace,
            )
        except DocumentStoreError as exc:
            code = (
                IndexErrorCode.SOURCE_MISSING
                if exc.code.value in ("not_found", "tombstone_conflict")
                else IndexErrorCode.INVALID_INTENT
            )
            raise IndexingError(code, f"stored source unavailable: {exc.code.value}") from None

    # -- delete ------------------------------------------------------------

    async def _reconcile_delete(self, intent: ReindexIntent) -> ReconcileOutcome:
        scope = {
            "server_id": intent.server_id,
            "app_id": intent.app_id,
            "namespace": intent.namespace.value,
            "doc_id": str(intent.doc_id),
        }
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT document_key, tombstone FROM knowledge_documents "
                        f"WHERE {_SCOPE_KEYS}"
                    ),
                    scope,
                )
            ).all()
            if not rows:
                # Idempotent retry: projection already gone; acknowledge.
                pass
            else:
                keys = [str(row.document_key) for row in rows]
                if any(not row.tombstone for row in rows):
                    # Phase 1 (durable): mark the delete on the projection
                    # rows and remove their chunks — stale retrieval results
                    # are gone from this commit onward, even if the crash
                    # window lands before phase 2.
                    await session.execute(
                        text(
                            "UPDATE knowledge_documents SET tombstone = true, "
                            "tombstoned_at = :tombstoned_at "
                            "WHERE document_key = ANY(:keys)"
                        ),
                        {
                            "tombstoned_at": datetime.now(timezone.utc),
                            "keys": keys,
                        },
                    )
                    await session.execute(
                        text(
                            "DELETE FROM knowledge_doc_chunks "
                            "WHERE document_key = ANY(:keys)"
                        ),
                        {"keys": keys},
                    )
                    await session.commit()
                # Phase 2: purge the tombstoned rows (and any chunkless
                # residue from a phase-1 crash), then acknowledge.
                await session.execute(
                    text("DELETE FROM knowledge_documents WHERE document_key = ANY(:keys)"),
                    {"keys": keys},
                )
                await session.commit()
        self._store.purge_intent(
            intent.doc_id,
            server_id=intent.server_id,
            app_id=intent.app_id,
            namespace=intent.namespace,
        )
        return ReconcileOutcome(
            doc_id=intent.doc_id,
            namespace=intent.namespace.value,
            op=intent.op,
            status=OutcomeStatus.ABSENT if not rows else OutcomeStatus.DELETED,
            revision=intent.revision,
            chunks=0,
        )

    # -- dispatch -----------------------------------------------------------

    async def _reconcile_intent(self, intent: ReindexIntent) -> ReconcileOutcome:
        if intent.op == "delete":
            return await self._reconcile_delete(intent)
        if intent.op == "upsert":
            return await self._reconcile_upsert(intent)
        raise IndexingError(
            IndexErrorCode.INVALID_INTENT,
            f"unknown reindex intent op {intent.op!r}",
        )


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector text literal for one embedding (insert-parameter form)."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


async def _reconcile_async(args: argparse.Namespace) -> int:
    from mai.database import create_app_engine, resolve_database_url

    engine: AsyncEngine | None = None
    try:
        engine, sessions = create_app_engine(resolve_database_url())
    except Exception as exc:  # noqa: BLE001 - usage/IO boundary
        _emit({"kind": "error", "code": "database_unavailable", "detail": type(exc).__name__})
        return 2
    try:
        store = DocumentStore(args.root)
        connection = parse_connection(
            {
                "schema_version": CONNECTION_SCHEMA_VERSION,
                "name": "chunk-index-embedding",
                "base_url": args.base_url,
                "credential_ref": args.credential_ref,
                "timeout_seconds": args.timeout_seconds,
                "headers": {
                    name: value
                    for name, _, value in (
                        header.partition("=") for header in (args.header or ())
                    )
                    if name
                },
            }
        )
        resolver = EnvResolver(args.env_file) if args.env_file else None
        indexer = ScopeIndexer(sessions, store, GatewayEmbedder(connection, resolver))
        outcomes = await indexer.reconcile_scope(
            server_id=uuid.UUID(args.server) if args.server else None,
            app_id=uuid.UUID(args.app) if args.app else None,
        )
        for outcome in outcomes:
            _emit(
                {
                    "kind": "intent",
                    "doc_id": str(outcome.doc_id),
                    "namespace": outcome.namespace,
                    "op": outcome.op,
                    "status": outcome.status.value,
                    "revision": outcome.revision,
                    "chunks": outcome.chunks,
                    "code": outcome.code.value if outcome.code else None,
                }
            )
        pending = sum(1 for outcome in outcomes if outcome.status is OutcomeStatus.PENDING)
        _emit(
            {
                "kind": "summary",
                "intents": len(outcomes),
                "pending": pending,
            }
        )
        return 0
    except (DocumentStoreError, ValueError) as exc:
        detail = str(exc)
        fields = getattr(exc, "fields", None)
        code = (
            "invalid_connection"
            if fields is not None
            else detail.split(":", 1)[0]
        )
        _emit({"kind": "error", "code": code, "detail": detail})
        return 1
    finally:
        await engine.dispose()


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--server", metavar="UUID", help="server scope id")
    group.add_argument(
        "--shared", action="store_true", help="explicit shared namespace scope"
    )
    parser.add_argument("--app", metavar="UUID", help="application scope id")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mai.knowledge.index",
        description="Section-aware chunk indexer: reconciles the durable "
        "doc-store reindex intents into the pgvector/FTS projection.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    reconcile = sub.add_parser("reconcile", help="reindex one scope's intent outbox")
    reconcile.add_argument("root", metavar="ROOT", help="explicit doc-store data root")
    _add_scope_args(reconcile)
    reconcile.add_argument(
        "--base-url",
        required=True,
        help="OpenAI-compatible endpoint root (without /v1 suffix unless "
        "the gateway already includes it)",
    )
    reconcile.add_argument(
        "--credential-ref",
        default=None,
        help="credential reference resolved through --env-file at request time",
    )
    reconcile.add_argument(
        "--env-file",
        default=None,
        help="explicit .env credential source (C03 rule 6: no discovery)",
    )
    reconcile.add_argument("--timeout-seconds", type=float, default=None)
    reconcile.add_argument(
        "--header",
        action="append",
        metavar="NAME=VALUE",
        help="extra endpoint header (repeatable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: exit 0 reconcile ran (pending listed) / 1 / 2."""
    args = _build_parser().parse_args(argv)
    if args.command == "reconcile":
        return asyncio.run(_reconcile_async(args))
    return 2  # pragma: no cover - argparse `required=True` guards this


if __name__ == "__main__":  # pragma: no cover - argparse glue
    sys.exit(main())
