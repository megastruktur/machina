"""Server-filtered hybrid (pgvector + FTS) document retrieval capability.

Card ``07-retrieval`` (service ``knowledge``), against contracts.md **C07**
rules 2/3/4 and the knowledge-packaging ADR (``5d6e0a1``) §4.2 dimension
contract, consuming the reviewed prerequisites ``07-vector-schema`` (the
``knowledge_documents`` / ``knowledge_doc_chunks`` projection),
``07-chunk-index`` (the :class:`~mai.knowledge.index.ScopeIndexer` that
fills the projection and the :class:`~mai.knowledge.index.ChunkEmbedder`
query-embedding seam with its frozen dimension constants), ``07-doc-store``
(canonical source revisions the citations resolve to) and ``03-dispatcher``
/ ``06-tool-bridge`` (the only invocation paths — this executor is
reachable exclusively as a trusted capability implementation behind
:class:`~mai.capabilities.dispatch.CapabilityDispatcher`).

- **C07 rule 2 (storage-boundary scope filter)** — the server filter is a
  SQL predicate on the projection rows (``server_id IS NOT DISTINCT FROM
  :server_id AND app_id IS NOT DISTINCT FROM :app_id AND tombstone =
  false``), never prompt-side trimming: the query planner excludes foreign
  scope rows, so a model can never be shown another server's chunks even
  through a crafted payload. The shared namespace (``server_id IS NULL``)
  is reachable only through an *explicit* payload scope choice
  (``scope="shared"``); the default ``scope="server"`` resolves to the
  :class:`~mai.policy.evaluate.ExecutionContext` ``server_id``. The
  payload carries **no target field** (C03 rule 2: the target vocabulary
  is the execution context), so a payload cannot divert or widen a query
  to a different server.
- **C07 rule 3 (dimension contract)** — the query embedding flows through
  the same pinned-model seam the indexer uses; a response whose length ≠
  the frozen 1536 is refused (``dimension_mismatch``) rather than compared
  against the index. The index rows themselves are CHECK-pinned by
  ``07-vector-schema``, so a matching consumer can never silently compare
  vectors across models or dims.
- **Hybrid ranking** — two bounded candidate pools over the *scoped* rows
  (pgvector cosine distance on the HNSW ``vector_cosine_ops`` index and
  PostgreSQL FTS ``ts_rank`` over the stored ``search_vector``, plain
  ``simple`` config exactly as the projection defines it) are fused with
  reciprocal-rank fusion (RRF, k=60): a chunk strong in only one signal
  still surfaces, a chunk strong in both outranks either single-signal
  match. Both pool sizes and the result count are bounded — excerpts, not
  the corpus (acceptance 2).
- **C07 rule 4 (provenance-preserving citations)** — every excerpt
  carries the full citation tuple (``document_key``, ``doc_id``,
  ``namespace``, ``revision``, ``sha256``, ``ord``) plus provenance and
  freshness (``source_kind``, ``origin``, ``origin_revision``,
  ``verification``, projection-owned ``observed_at``); a length-bounded
  excerpt is marked ``truncated`` and keeps every identifier, so a cut
  excerpt remains traceable to its exact source revision and offset.
- **Unavailable ≠ zero results (acceptance 2)** — storage failure
  (``index_unavailable``) and query-embedding failure
  (``embedding_unavailable`` / ``dimension_mismatch``) are distinct
  ``status="unavailable"`` outcomes with a value-free error type name; a
  query that ran and matched nothing is ``status="ok"`` with an empty
  excerpt list. An empty answer is never fabricated from a failed one.
- **Value-free discipline** — rejections carry stable codes and field
  names; storage exception *messages* (which may quote data) never
  surface — only the exception type name, mirroring the dispatcher's
  honesty rule. Excerpt content is legitimate retrieval output; errors
  contain none.

Effect class ``read``: retrieval changes no observable state, so the
policy layer requires no approval decision in either prompting mode —
while every grant/scope/policy gate still applies unchanged ahead of
this executor (pinned by tests against the real dispatcher and bridge).
The module reads no process environment and no clock except the
projection-owned ``observed_at`` it returns; the network seam is the
query embedder alone.

Production entrypoint (exit contract 0 query ran — the JSON status
distinguishes ``ok`` / ``unavailable`` / zero results; 1 structured
rejection; 2 usage or IO error)::

    uv run python -m mai.knowledge.retrieve query (--server ID | --shared) [--app ID] \
        [--namespace docs|runbooks] --query TEXT --base-url URL \
        [--credential-ref NAME] [--env-file PATH] [--timeout-seconds N] \
        [--limit N] [--max-excerpt-chars N]

The database URL comes from ``MAI_DATABASE_URL`` (``mai.database``); the
embedding credential reference resolves through the ``03-credentials``
``.env`` resolver at request time — secret material never appears in
output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.knowledge.documents import DocumentNamespace
from mai.knowledge.index import (
    EMBEDDING_DIMENSIONS,
    ChunkEmbedder,
    GatewayEmbedder,
    IndexErrorCode,
    IndexingError,
)
from mai.plugins.registry import CapabilityDescriptor, parse_descriptor
from mai.policy.evaluate import ExecutionContext

__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_EXCERPT_CHARS",
    "MAX_EXCERPT_CHARS",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "MIN_EXCERPT_CHARS",
    "RRF_K",
    "DocumentRetrievalCapability",
    "Excerpt",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievalScope",
    "RetrievalStatus",
    "RetrieveError",
    "RetrieveErrorCode",
    "ScopeRetriever",
    "descriptor",
    "main",
    "query_from_payload",
    "result_document",
]

#: The capability slug this module owns (registry identifier shape; C03
#: rule 1). The ``docs.`` prefix names the canonical docs/runbooks
#: collections (C02 rule 2); web search is a sibling capability.
CAPABILITY_ID = "docs.retrieve"

#: Query text bound (the FTS dictionary is bounded work; the embedding
#: request stays a single small input).
MAX_QUERY_CHARS = 2048

#: Default and bounds for the excerpt count — bounded relevant excerpts,
#: never the whole corpus (acceptance 2).
DEFAULT_LIMIT = 5
MAX_LIMIT = 20

#: Default and bounds for one excerpt's character budget. The floor keeps
#: an excerpt a usable evidence unit; the ceiling matches the chunk cap
#: (``07-chunk-index`` MAX_CHUNK_CHARS) so a whole chunk fits.
DEFAULT_MAX_EXCERPT_CHARS = 1200
MIN_EXCERPT_CHARS = 200
MAX_EXCERPT_CHARS = 4000

#: Reciprocal-rank fusion constant (standard k=60): robust to the two
#: signals living on incomparable scales (cosine distance vs ts_rank).
RRF_K = 60

#: Candidate-pool sizing per signal: 4x the requested excerpt count with a
#: 16 floor, hard-capped — the fusion inputs stay bounded regardless of
#: corpus size.
_POOL_FACTOR = 4
_POOL_FLOOR = 16
_POOL_CAP = 80


class RetrievalScope(StrEnum):
    """The only two scope choices a payload may name.

    ``server`` resolves to the execution context's ``server_id``;
    ``shared`` is the explicit shared namespace (``server_id IS NULL``).
    No payload field can name an arbitrary server.
    """

    SERVER = "server"
    SHARED = "shared"


class RetrievalStatus(StrEnum):
    """The honest answer of one retrieval attempt."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


class RetrieveErrorCode(StrEnum):
    """Stable machine-readable retrieval rejection/outage codes."""

    INDEX_UNAVAILABLE = "index_unavailable"
    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    DIMENSION_MISMATCH = "dimension_mismatch"
    INVALID_PAYLOAD = "invalid_payload"


class RetrieveError(ValueError):
    """One structured, value-free payload rejection (defense in depth).

    Raised only for malformed *direct* executor calls; the
    ``06-tool-bridge`` schema gate denies the same shapes before dispatch,
    so the dispatcher's normal path never reaches these.
    """

    def __init__(self, code: RetrieveErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """One validated retrieval request (capability payload shape)."""

    query: str
    scope: RetrievalScope
    namespace: DocumentNamespace | None
    limit: int
    max_excerpt_chars: int


@dataclass(frozen=True, slots=True)
class Excerpt:
    """One provenance-preserving citation with its bounded excerpt text.

    The citation tuple (``document_key``, ``doc_id``, ``namespace``,
    ``revision``, ``sha256``, ``ord``) identifies the exact source
    revision and chunk offset even when ``truncated`` cut the text
    (C07 rule 4).
    """

    document_key: uuid.UUID
    doc_id: uuid.UUID
    namespace: str
    revision: int
    sha256: str
    ord: int
    source_kind: str
    origin: str
    origin_revision: str | None
    verification: str
    observed_at: datetime | None
    score: float
    vector_rank: int | None
    fts_rank: int | None
    excerpt: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """One honest retrieval answer, scoped and value-free on failure.

    ``status`` is ``ok`` only when the query actually ran (zero matches
    included); ``unavailable`` names which seam failed via ``code`` and
    the exception *type* only. ``scope`` echoes the resolved scope so a
    transcript proves which storage filter was applied.
    """

    status: RetrievalStatus
    code: RetrieveErrorCode | None
    scope: str
    server_id: uuid.UUID | None
    excerpts: tuple[Excerpt, ...]
    error_type: str | None = None


# ---------------------------------------------------------------------------
# Payload validation (defense in depth; the bridge schema gate is primary)
# ---------------------------------------------------------------------------


def _reject(code: RetrieveErrorCode, detail: str) -> RetrieveError:
    return RetrieveError(code, detail)


def query_from_payload(
    payload: Mapping[str, object],
    *,
    default_limit: int = DEFAULT_LIMIT,
    default_max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
) -> RetrievalQuery:
    """Validate one capability payload into a :class:`RetrievalQuery`.

    Strict and fail-closed exactly like the producer capabilities: unknown
    keys are rejected (``additionalProperties: false`` in the descriptor),
    so no smuggled field can reach the storage query.
    """
    if not isinstance(payload, Mapping):
        raise _reject(
            RetrieveErrorCode.INVALID_PAYLOAD,
            "payload must be a JSON object (mapping)",
        )
    allowed = {"query", "scope", "namespace", "limit", "max_excerpt_chars"}
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise _reject(
            RetrieveErrorCode.INVALID_PAYLOAD,
            f"unknown payload fields: {', '.join(unknown)}",
        )

    query = payload.get("query")
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > MAX_QUERY_CHARS
        or "\x00" in query
    ):
        raise _reject(
            RetrieveErrorCode.INVALID_PAYLOAD,
            f"query must be a non-empty string of at most "
            f"{MAX_QUERY_CHARS} characters without NUL bytes",
        )

    scope = payload.get("scope", RetrievalScope.SERVER.value)
    if not isinstance(scope, str) or scope not in (
        RetrievalScope.SERVER.value,
        RetrievalScope.SHARED.value,
    ):
        raise _reject(
            RetrieveErrorCode.INVALID_PAYLOAD,
            f"scope must be one of {sorted(s.value for s in RetrievalScope)}",
        )

    namespace = payload.get("namespace")
    if namespace is not None:
        if not isinstance(namespace, str) or namespace not in (
            DocumentNamespace.DOCS.value,
            DocumentNamespace.RUNBOOKS.value,
        ):
            raise _reject(
                RetrieveErrorCode.INVALID_PAYLOAD,
                "namespace must be one of "
                f"{sorted(ns.value for ns in DocumentNamespace)} or absent",
            )
        namespace = DocumentNamespace(namespace)

    limit = payload.get("limit", default_limit)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_LIMIT
    ):
        raise _reject(
            RetrieveErrorCode.INVALID_PAYLOAD,
            f"limit must be an int in [1, {MAX_LIMIT}]",
        )

    max_excerpt = payload.get(
        "max_excerpt_chars", default_max_excerpt_chars
    )
    if (
        isinstance(max_excerpt, bool)
        or not isinstance(max_excerpt, int)
        or not MIN_EXCERPT_CHARS <= max_excerpt <= MAX_EXCERPT_CHARS
    ):
        raise _reject(
            RetrieveErrorCode.INVALID_PAYLOAD,
            f"max_excerpt_chars must be an int in "
            f"[{MIN_EXCERPT_CHARS}, {MAX_EXCERPT_CHARS}]",
        )

    return RetrievalQuery(
        query=query,
        scope=RetrievalScope(scope),
        namespace=namespace,
        limit=limit,
        max_excerpt_chars=max_excerpt,
    )


# ---------------------------------------------------------------------------
# Hybrid scope retriever: the storage-boundary query engine
# ---------------------------------------------------------------------------


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector text literal for one embedding (parameter form).

    Local mirror of the ``07-chunk-index`` insert form: the literal is
    bound as text and cast in SQL (``CAST(:param AS vector)``), so the
    query parameter never needs driver-side vector adaptation.
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


_SCOPE_FILTER = (
    "d.server_id IS NOT DISTINCT FROM :server_id AND "
    "d.app_id IS NOT DISTINCT FROM :app_id AND "
    "d.tombstone = false AND "
    "(CAST(:namespace AS text) IS NULL OR d.namespace = CAST(:namespace AS text))"
)

_HYBRID_SQL = f"""
WITH scoped AS (
    SELECT d.document_key, d.doc_id, d.namespace, d.revision, d.sha256,
           d.source_kind, d.origin, d.origin_revision, d.verification,
           d.observed_at
    FROM knowledge_documents d
    WHERE {_SCOPE_FILTER}
),
vec AS (
    SELECT c.document_key, c.ord,
           row_number() OVER (
               ORDER BY c.embedding <=> CAST(:query_embedding AS vector)
           ) AS vector_rank
    FROM knowledge_doc_chunks c
    JOIN scoped s ON s.document_key = c.document_key
    ORDER BY c.embedding <=> CAST(:query_embedding AS vector)
    LIMIT :pool
),
fts AS (
    SELECT c.document_key, c.ord,
           row_number() OVER (
               ORDER BY ts_rank(
                   c.search_vector, plainto_tsquery('simple', :query_text)
               ) DESC
           ) AS fts_rank
    FROM knowledge_doc_chunks c
    JOIN scoped s ON s.document_key = c.document_key
    WHERE c.search_vector @@ plainto_tsquery('simple', :query_text)
    ORDER BY ts_rank(
        c.search_vector, plainto_tsquery('simple', :query_text)
    ) DESC
    LIMIT :pool
),
merged AS (
    SELECT COALESCE(v.document_key, f.document_key) AS document_key,
           COALESCE(v.ord, f.ord) AS ord,
           v.vector_rank, f.fts_rank
    FROM vec v
    FULL OUTER JOIN fts f
        ON f.document_key = v.document_key AND f.ord = v.ord
)
SELECT m.document_key, m.ord, m.vector_rank, m.fts_rank,
       c.content, s.doc_id, s.namespace, s.revision, s.sha256,
       s.source_kind, s.origin, s.origin_revision, s.verification,
       s.observed_at,
       COALESCE(1.0 / (:rrf_k + m.vector_rank), 0.0)
       + COALESCE(1.0 / (:rrf_k + m.fts_rank), 0.0) AS rrf
FROM merged m
JOIN knowledge_doc_chunks c
    ON c.document_key = m.document_key AND c.ord = m.ord
JOIN scoped s ON s.document_key = m.document_key
ORDER BY rrf DESC, m.document_key, m.ord
LIMIT :limit
"""


class ScopeRetriever:
    """Hybrid pgvector+FTS retrieval over one explicit scope.

    The storage consumer side of C07 rule 2: the scope filter is part of
    the SQL (the ``scoped`` CTE), so foreign-server rows are excluded by
    the query, never filtered from a wider result set. The query embedder
    is the same pinned seam the indexer uses; a wrong-length response is
    a refusal, not a comparison (rule 3).
    """

    def __init__(self, sessions: async_sessionmaker, embedder: ChunkEmbedder) -> None:
        self._sessions = sessions
        self._embedder = embedder

    async def retrieve(
        self,
        *,
        server_id: uuid.UUID | None,
        app_id: uuid.UUID | None = None,
        namespace: DocumentNamespace | None = None,
        query: str,
        limit: int = DEFAULT_LIMIT,
        max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    ) -> RetrievalResult:
        """Answer one bounded hybrid query for one scope.

        ``server_id=None`` is the explicit shared namespace. ``available``
        outcomes are decided only by the query result; storage and
        embedding-seam failures are ``unavailable`` with a distinct code.
        """
        scope_label = (
            RetrievalScope.SHARED.value
            if server_id is None
            else RetrievalScope.SERVER.value
        )
        # Embed BEFORE touching the index: an unavailable embedding seam
        # is an honest outage, never a silent FTS-only degradation of the
        # frozen hybrid ranking.
        try:
            vectors = await self._embedder.embed([query])
        except IndexingError as exc:
            code = (
                RetrieveErrorCode.DIMENSION_MISMATCH
                if exc.code is IndexErrorCode.DIMENSION_MISMATCH
                else RetrieveErrorCode.EMBEDDING_UNAVAILABLE
            )
            return RetrievalResult(
                status=RetrievalStatus.UNAVAILABLE,
                code=code,
                scope=scope_label,
                server_id=server_id,
                excerpts=(),
                error_type=type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 - classified, value-free
            return RetrievalResult(
                status=RetrievalStatus.UNAVAILABLE,
                code=RetrieveErrorCode.EMBEDDING_UNAVAILABLE,
                scope=scope_label,
                server_id=server_id,
                excerpts=(),
                error_type=type(exc).__name__,
            )
        if len(vectors) != 1 or len(vectors[0]) != EMBEDDING_DIMENSIONS:
            return RetrievalResult(
                status=RetrievalStatus.UNAVAILABLE,
                code=RetrieveErrorCode.DIMENSION_MISMATCH,
                scope=scope_label,
                server_id=server_id,
                excerpts=(),
                error_type="IndexingError",
            )
        pool = min(max(limit * _POOL_FACTOR, _POOL_FLOOR), _POOL_CAP)
        params = {
            "server_id": server_id,
            "app_id": app_id,
            "namespace": namespace.value if namespace is not None else None,
            "query_embedding": _vector_literal(vectors[0]),
            "query_text": query,
            "rrf_k": RRF_K,
            "pool": pool,
            "limit": limit,
        }
        try:
            async with self._sessions() as session:
                rows = (await session.execute(text(_HYBRID_SQL), params)).all()
        except Exception as exc:  # noqa: BLE001 - classified, value-free
            return RetrievalResult(
                status=RetrievalStatus.UNAVAILABLE,
                code=RetrieveErrorCode.INDEX_UNAVAILABLE,
                scope=scope_label,
                server_id=server_id,
                excerpts=(),
                error_type=type(exc).__name__,
            )
        excerpts = tuple(
            self._excerpt(row, max_excerpt_chars) for row in rows
        )
        return RetrievalResult(
            status=RetrievalStatus.OK,
            code=None,
            scope=scope_label,
            server_id=server_id,
            excerpts=excerpts,
        )

    @staticmethod
    def _excerpt(row, max_excerpt_chars: int) -> Excerpt:
        content: str = row.content
        truncated = len(content) > max_excerpt_chars
        text_out = content[:max_excerpt_chars] if truncated else content
        return Excerpt(
            document_key=row.document_key,
            doc_id=row.doc_id,
            namespace=row.namespace,
            revision=row.revision,
            sha256=row.sha256,
            ord=row.ord,
            source_kind=row.source_kind,
            origin=row.origin,
            origin_revision=row.origin_revision,
            verification=row.verification,
            observed_at=row.observed_at,
            score=round(float(row.rrf), 6),
            vector_rank=row.vector_rank,
            fts_rank=row.fts_rank,
            excerpt=text_out,
            truncated=truncated,
        )


# ---------------------------------------------------------------------------
# Result document (frozen shape, stable key order via json.dumps)
# ---------------------------------------------------------------------------


def result_document(result: RetrievalResult) -> dict[str, object]:
    """The frozen JSON document of one retrieval result."""
    if result.status is RetrievalStatus.UNAVAILABLE:
        return {
            "status": result.status.value,
            "code": result.code.value if result.code is not None else None,
            "scope": result.scope,
            "server_id": (
                None if result.server_id is None else str(result.server_id)
            ),
            "excerpts": [],
            "error_type": result.error_type,
        }
    return {
        "status": result.status.value,
        "code": None,
        "scope": result.scope,
        "server_id": (
            None if result.server_id is None else str(result.server_id)
        ),
        "excerpts": [
            {
                "document_key": str(excerpt.document_key),
                "doc_id": str(excerpt.doc_id),
                "namespace": excerpt.namespace,
                "revision": excerpt.revision,
                "sha256": excerpt.sha256,
                "ord": excerpt.ord,
                "citation": (
                    f"{excerpt.namespace}:{excerpt.doc_id}"
                    f"#r{excerpt.revision}:{excerpt.ord}"
                ),
                "source_kind": excerpt.source_kind,
                "origin": excerpt.origin,
                "origin_revision": excerpt.origin_revision,
                "verification": excerpt.verification,
                "observed_at": (
                    None
                    if excerpt.observed_at is None
                    else excerpt.observed_at.isoformat()
                ),
                "score": excerpt.score,
                "vector_rank": excerpt.vector_rank,
                "fts_rank": excerpt.fts_rank,
                "excerpt": excerpt.excerpt,
                "truncated": excerpt.truncated,
            }
            for excerpt in result.excerpts
        ],
        "error_type": None,
    }


# ---------------------------------------------------------------------------
# The trusted capability implementation (dispatcher-gated)
# ---------------------------------------------------------------------------


class DocumentRetrievalCapability:
    """``docs.retrieve`` executor: one gated call → one honest result.

    Reachable only through the dispatcher (C03 rule 7); performs no
    policy decision of its own — the :class:`ExecutionContext` supplies
    the only server vocabulary (C03 rule 2) and the payload chooses only
    ``server`` (context target) or the explicit ``shared`` namespace.
    """

    def __init__(
        self,
        retriever: ScopeRetriever,
        *,
        default_limit: int = DEFAULT_LIMIT,
        default_max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    ) -> None:
        if not isinstance(retriever, ScopeRetriever):
            raise RetrieveError(
                RetrieveErrorCode.INVALID_PAYLOAD,
                "retriever must be a ScopeRetriever",
            )
        self._retriever = retriever
        self._default_limit = default_limit
        self._default_max_excerpt = default_max_excerpt_chars

    def __repr__(self) -> str:
        return f"<DocumentRetrievalCapability retriever={self._retriever!r}>"

    async def __call__(self, call: CapabilityCall) -> ExecutorResult:
        """Execute one gated call (see module docstring for the order)."""
        if not isinstance(call, CapabilityCall):
            raise RetrieveError(
                RetrieveErrorCode.INVALID_PAYLOAD,
                "executor input must be a dispatcher CapabilityCall",
            )
        context = call.context
        if not isinstance(context, ExecutionContext):
            raise RetrieveError(
                RetrieveErrorCode.INVALID_PAYLOAD,
                "call context must be an ExecutionContext",
            )
        request = query_from_payload(
            call.payload,
            default_limit=self._default_limit,
            default_max_excerpt_chars=self._default_max_excerpt,
        )
        if request.scope is RetrievalScope.SERVER:
            server_id: uuid.UUID | None = context.server_id
        else:
            server_id = None
        result = await self._retriever.retrieve(
            server_id=server_id,
            app_id=None,
            namespace=request.namespace,
            query=request.query,
            limit=request.limit,
            max_excerpt_chars=request.max_excerpt_chars,
        )
        return ExecutorResult(
            ok=result.status is RetrievalStatus.OK,
            output=json.dumps(
                result_document(result), sort_keys=True, ensure_ascii=True
            ),
        )


# ---------------------------------------------------------------------------
# Capability descriptor (C03 rule 1 frozen vocabulary)
# ---------------------------------------------------------------------------


def descriptor() -> CapabilityDescriptor:
    """The validated capability descriptor (C03 rule 1 vocabulary).

    ``effect_class="read"`` is the load-bearing field: retrieval changes
    no observable state, so the policy layer requires no approval
    decision in either prompting mode — while every grant/scope/policy
    gate still applies unchanged. ``additionalProperties: false`` plus
    the absent target field pin the payload surface: no field can name a
    server, so scope is always the context target or the explicit shared
    namespace.
    """
    return parse_descriptor(
        {
            "identifier": CAPABILITY_ID,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_QUERY_CHARS,
                        "description": (
                            "Natural-language or keyword query text; "
                            "embedded with the frozen pinned model and "
                            "matched by FTS under the simple config."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": sorted(s.value for s in RetrievalScope),
                        "default": RetrievalScope.SERVER.value,
                        "description": (
                            "server = the execution context's server_id "
                            "(default); shared = the explicit shared "
                            "namespace. A payload can never name another "
                            "server."
                        ),
                    },
                    "namespace": {
                        "type": "string",
                        "enum": sorted(ns.value for ns in DocumentNamespace),
                        "description": (
                            "Optional collection filter; absent searches "
                            "docs and runbooks of the scope."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIMIT,
                        "default": DEFAULT_LIMIT,
                        "description": (
                            "Bounded excerpt count; the corpus is never "
                            "returned whole."
                        ),
                    },
                    "max_excerpt_chars": {
                        "type": "integer",
                        "minimum": MIN_EXCERPT_CHARS,
                        "maximum": MAX_EXCERPT_CHARS,
                        "default": DEFAULT_MAX_EXCERPT_CHARS,
                        "description": (
                            "Per-excerpt character budget; truncated "
                            "excerpts keep their full citation tuple."
                        ),
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "required": ["status", "scope", "excerpts"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": sorted(s.value for s in RetrievalStatus),
                    },
                    "code": {
                        "type": ["string", "null"],
                        "enum": [None]
                        + sorted(code.value for code in RetrieveErrorCode),
                    },
                    "scope": {"type": "string"},
                    "server_id": {"type": ["string", "null"]},
                    "excerpts": {"type": "array"},
                    "error_type": {"type": ["string", "null"]},
                },
            },
            "execution_environments": ["controller"],
            "effect_class": "read",
            "resource_scope": [],
            "required_grants": [CAPABILITY_ID],
        }
    )


# ---------------------------------------------------------------------------
# Production entrypoint
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))


async def _query_async(args: argparse.Namespace) -> int:
    from mai.agents.models import (
        SCHEMA_VERSION as CONNECTION_SCHEMA_VERSION,
        parse_connection,
    )
    from mai.credentials.env import EnvResolver
    from mai.database import create_app_engine, resolve_database_url

    engine: AsyncEngine | None = None
    try:
        engine, sessions = create_app_engine(resolve_database_url())
    except Exception as exc:  # noqa: BLE001 - usage/IO boundary
        _emit(
            {
                "kind": "error",
                "code": "database_unavailable",
                "detail": type(exc).__name__,
            }
        )
        return 2
    try:
        connection = parse_connection(
            {
                "schema_version": CONNECTION_SCHEMA_VERSION,
                "name": "retrieval-query-embedding",
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
        retriever = ScopeRetriever(
            sessions, GatewayEmbedder(connection, resolver)
        )
        server_id = uuid.UUID(args.server) if args.server else None
        app_id = uuid.UUID(args.app) if args.app else None
        namespace = (
            DocumentNamespace(args.namespace) if args.namespace else None
        )
        result = await retriever.retrieve(
            server_id=server_id,
            app_id=app_id,
            namespace=namespace,
            query=args.query,
            limit=args.limit,
            max_excerpt_chars=args.max_excerpt_chars,
        )
        _emit(result_document(result))
        return 0
    except (IndexingError, ValueError) as exc:
        detail = str(exc)
        fields = getattr(exc, "fields", None)
        code = (
            "invalid_connection"
            if fields is not None
            else (
                exc.code.value
                if isinstance(exc, (IndexingError, RetrieveError))
                else detail.split(":", 1)[0]
            )
        )
        _emit({"kind": "error", "code": code, "detail": detail})
        return 1
    finally:
        await engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mai.knowledge.retrieve",
        description="Hybrid pgvector+FTS document retrieval over one "
        "explicit server or shared scope.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query", help="run one bounded scope query")
    group = query.add_mutually_exclusive_group()
    group.add_argument("--server", metavar="UUID", help="server scope id")
    group.add_argument(
        "--shared", action="store_true", help="explicit shared namespace scope"
    )
    query.add_argument("--app", metavar="UUID", help="application scope id")
    query.add_argument(
        "--namespace",
        choices=[ns.value for ns in DocumentNamespace],
        default=None,
        help="optional collection filter",
    )
    query.add_argument("--query", required=True, help="query text")
    query.add_argument(
        "--base-url",
        required=True,
        help="OpenAI-compatible endpoint root (without /v1 suffix unless "
        "the gateway already includes it)",
    )
    query.add_argument(
        "--credential-ref",
        default=None,
        help="credential reference resolved through --env-file at request time",
    )
    query.add_argument(
        "--env-file",
        default=None,
        help="explicit .env credential source (C03 rule 6: no discovery)",
    )
    query.add_argument("--timeout-seconds", type=float, default=None)
    query.add_argument(
        "--header",
        action="append",
        metavar="NAME=VALUE",
        help="extra endpoint header (repeatable)",
    )
    query.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    query.add_argument(
        "--max-excerpt-chars", type=int, default=DEFAULT_MAX_EXCERPT_CHARS
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: exit 0 query ran (JSON status distinguishes
    ok / unavailable / zero results) / 1 structured rejection / 2 usage."""
    args = _build_parser().parse_args(argv)
    if args.command == "query":
        return asyncio.run(_query_async(args))
    return 2  # pragma: no cover - argparse `required=True` guards this


if __name__ == "__main__":  # pragma: no cover - argparse glue
    sys.exit(main())
