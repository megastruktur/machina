"""Documents HTTP API: canonical docs/runbooks, cited retrieval, index
status/reconcile and run-evidence generation (C01, C02, C07, C08).

Ownership map (contracts.md C01/C02/C07/C08; producers 07-doc-store,
07-vector-schema, 07-chunk-index, 07-retrieval, 07-doc-generation,
07-deep-research; SERVICES ``mai-api`` row):

- Identity is the opaque UUID only (C01 rule 1): ``POST /api/v1/docs``
  mints a fresh ``doc_id`` server-side; every later operation is addressed
  by that UUID in the path. Scopes are the frozen canonical triple
  (``server_id`` + optional ``app_id``, or the explicit shared namespace);
  an ``app_id`` without ``server_id`` is a payload rejection, never a
  guessed scope.
- The principal comes from the validated session only (C01 rule 2); the
  request security boundary is owned by :mod:`mai.api.auth` and this
  router joins it — the same layer order (session → origin → CSRF, ADR §5)
  and the same ``BoundaryError`` rendering registered by ``register_auth``.
  GET is a safe method and applies layer 1 only; every POST/PUT/DELETE
  applies all three layers, mirroring ``mai.api.servers``/``mai.api.models``.
- The API never touches canonical files or SQL directly except through
  the reviewed producers: content truth is
  :class:`mai.knowledge.documents.DocumentStore` (hash-verified reads,
  revision-checked writes, durable reindex outbox); list/index truth is
  the ``07-vector-schema`` projection (``knowledge_documents``), the
  designed UI read path (C02 W2: the UI reads the projection, the file is
  the authority).
- Optimistic concurrency (C01 rule 6): ``PUT``/``DELETE`` carry
  ``expected_revision``; a stale caller is rejected with a structured
  ``409 conflict`` BEFORE any write. Provenance (C07 rule 1) and the
  outcome/hypothesis boundary (C07 rule 5) travel verbatim from the
  store's frozen vocabulary.
- The retrieval route answers through :class:`~mai.knowledge.retrieve.
  ScopeRetriever` directly: this is the trusted admin surface (the
  session IS the authority, like the profile surface), so the admin may
  name any enrolled scope; the server filter is still applied at the
  storage boundary (C07 rule 2), never post-filtered. An unavailable
  index/embedding seam is an explicit ``status: "unavailable"`` answer
  naming the failed seam, never an empty-success (AC2).
- The index routes expose the durable outbox truth: live intents are
  ``pending`` (explicit, not "unknown"), per-intent reconcile outcomes
  name ``indexed``/``unchanged``/``deleted``/``absent``/``pending`` with
  the producer's stable codes on failure (AC2).
- The generate route is the 07-doc-generation surface over the same
  canonical store: ``preview`` validates/classifies/renders without any
  store touch; ``commit`` writes revision-checked through
  :class:`~mai.knowledge.generate.DocGenerator` and honestly reports the
  reindex outcome (``null`` when no indexer is wired — the durable intent
  converges later, never a fabricated "reindexed").
- The research route runs the reviewed 07-deep-research composition
  synchronously over ONE scoped channel: ``docs.retrieve`` through the
  ONE dispatcher boundary with current grants/policy/adoption from app
  state (the workflow's own tests compose limited dispatchers; API-side
  composition is this surface's duty — the worker executor module never
  self-wires, and no worker card owns the API host). Channels the API
  process cannot truthfully provide (web search/pages, host logs), child
  runs (worker-owned) and model synthesis (worker-owned model rounds)
  are honest pre-flight ``503 unavailable`` rejections, never silent
  degradations; a settled run is reported truthfully (``succeeded`` /
  ``failed`` + incident reference) with every step and citation.

Route/doc listeners stay off (G1 minimal unauthenticated surface); the
schema is produced in-process via ``app.openapi()`` for the central
exporter owned by the ``openapi-client`` resource.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.api.servers import resolve_profiles_root
from mai.auth.sessions import hash_token
from mai.contracts.server import parse_server_profile_text
from mai.knowledge.documents import (
    DocumentNamespace,
    DocumentStore,
    DocumentStoreError,
    DocumentStoreErrorCode,
    ReindexIntent,
    SourceKind,
    StoredDocument,
    VerificationStatus,
)
from mai.knowledge.generate import (
    DocGenerationError,
    DocGenerator,
    classify,
    parse_job_evidence,
    render_document,
)
from mai.knowledge.index import ReconcileOutcome, ScopeIndexer
from mai.knowledge.retrieve import (
    DEFAULT_LIMIT as RETRIEVAL_DEFAULT_LIMIT,
    DEFAULT_MAX_EXCERPT_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_LIMIT as RETRIEVAL_MAX_LIMIT,
    MAX_QUERY_CHARS,
    MIN_EXCERPT_CHARS,
    RetrievalStatus,
    ScopeRetriever,
    result_document,
)
from mai.policy.evaluate import ExecutionContext, PolicyMode
from mai.research.workflow import (
    ResearchError,
    ResearchWorkflow,
    report_document,
    request_from_payload,
)

__all__ = [
    "EMBEDDINGS_BASE_URL_VAR",
    "EMBEDDINGS_CREDENTIAL_REF_VAR",
    "register_documents",
]

#: OpenAI-compatible embeddings endpoint root (knowledge-packaging ADR §E1
#: vocabulary: ``{base_url}/embeddings``, never ``/chat/completions``). The
#: retrieval/reconcile surfaces fail closed with ``503 unavailable`` when
#: it is absent; there is no discovered default.
EMBEDDINGS_BASE_URL_VAR: Final = "MAI_EMBEDDINGS_BASE_URL"
#: Optional credential *reference name* resolved through the app's
#: configured ``.env`` resolver (``MAI_ENV_FILE``) at request time; absent
#: means the endpoint declares itself credential-free.
EMBEDDINGS_CREDENTIAL_REF_VAR: Final = "MAI_EMBEDDINGS_CREDENTIAL_REF"

#: List page bounds (C01 rule 5: list endpoints are bounded/paginated).
DEFAULT_PAGE_LIMIT: Final = 50
MAX_PAGE_LIMIT: Final = 200

#: The only capability channel the API research surface truthfully
#: provides in-process.
RESEARCH_CAPABILITY: Final = "docs.retrieve"

#: Error-envelope field marker for payload-scope problems.
_SCOPE_FIELDS: Final[dict[str, str]] = {
    "scope": "name exactly one of server_id or shared"
}

#: Document-store rejections that are caller validation, not canonical
#: state (mapped to 422 with the producer's stable code; the store owns
#: the byte/UTF-8 bounds and refuses secrets/escapes before any effect).
_VALIDATION_REJECTIONS: Final[frozenset[DocumentStoreErrorCode]] = frozenset(
    {
        DocumentStoreErrorCode.INVALID_SERVER_ID,
        DocumentStoreErrorCode.APP_REQUIRES_SERVER,
        DocumentStoreErrorCode.INVALID_APP_ID,
        DocumentStoreErrorCode.INVALID_DOC_ID,
        DocumentStoreErrorCode.INVALID_NAMESPACE,
        DocumentStoreErrorCode.INVALID_REVISION,
        DocumentStoreErrorCode.INVALID_PROVENANCE,
        DocumentStoreErrorCode.INVALID_DOCUMENT,
        DocumentStoreErrorCode.DOCUMENT_TOO_LARGE,
        DocumentStoreErrorCode.PATH_ESCAPE,
        DocumentStoreErrorCode.SECRET_FILE_FORBIDDEN,
        DocumentStoreErrorCode.NOT_A_FILE,
        DocumentStoreErrorCode.SYMLINK_FORBIDDEN,
        DocumentStoreErrorCode.NOT_READABLE,
        DocumentStoreErrorCode.INTENT_INVALID,
    }
)


# ---------------------------------------------------------------------------
# Lifespan composition (built in mai.api.app; consumed here)
# ---------------------------------------------------------------------------


class KnowledgeServices:
    """The docs-surface service bundle built once in the app lifespan.

    ``retriever``/``indexer`` exist only when the database engine AND a
    valid embeddings connection were configured; every route through them
    fails closed with ``503 unavailable`` when absent. ``workflow`` is
    the deep-research composition over the one in-process channel; it
    additionally requires the grant/policy and profile stores.
    """

    def __init__(
        self,
        *,
        store: DocumentStore,
        retriever: ScopeRetriever | None,
        indexer: ScopeIndexer | None,
        workflow: ResearchWorkflow | None,
    ) -> None:
        self.store = store
        self.retriever = retriever
        self.indexer = indexer
        self.workflow = workflow


def _services(request: Request) -> KnowledgeServices:
    services = getattr(request.app.state, "knowledge", None)
    if services is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "the canonical document service is unavailable",
        )
    return services


def _embedding_services(request: Request) -> tuple[KnowledgeServices, ScopeRetriever]:
    services = _services(request)
    if services.retriever is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "the document index is unavailable (database or embeddings "
            "endpoint not configured)",
        )
    return services, services.retriever


# ---------------------------------------------------------------------------
# Wire shapes (transport only; field truth is the producers)
# ---------------------------------------------------------------------------


class ScopeBody(BaseModel):
    """One canonical scope (C07 rule 1): server[/app] or explicit shared.

    Exactly one of ``server_id`` or ``shared`` is required; ``app_id``
    requires ``server_id``. The store re-validates identity at the
    storage boundary.
    """

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID | None = None
    app_id: uuid.UUID | None = None
    shared: bool = False


class DocumentCreateBody(ScopeBody):
    """Create body: either authored ``content`` or an explicit import path.

    ``source_path`` is the 07-doc-store outside seam: read-only,
    regular-file only, credential-material names refused — a rejected
    import materializes nothing. ``verification`` is the C07 rule 5
    outcome/hypothesis vocabulary.
    """

    model_config = ConfigDict(extra="forbid")

    namespace: DocumentNamespace = DocumentNamespace.DOCS
    content: str | None = Field(default=None, min_length=1)
    source_path: str | None = Field(default=None, min_length=1, max_length=4096)
    origin_revision: str | None = Field(default=None, min_length=1, max_length=128)
    verification: VerificationStatus = VerificationStatus.UNVERIFIED

    def source_kind(self) -> SourceKind:
        return SourceKind.IMPORTED if self.source_path is not None else SourceKind.AUTHORED


class DocumentEditBody(ScopeBody):
    """Edit body: full-content replace with optimistic concurrency."""

    model_config = ConfigDict(extra="forbid")

    namespace: DocumentNamespace = DocumentNamespace.DOCS
    content: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)


class DocumentDeleteBody(ScopeBody):
    """Delete body: revision-checked removal with a durable tombstone."""

    model_config = ConfigDict(extra="forbid")

    namespace: DocumentNamespace = DocumentNamespace.DOCS
    expected_revision: int = Field(ge=0)


class RetrievalBody(ScopeBody):
    """Hybrid retrieval body (C07 rules 2–4).

    The admin session may name any enrolled scope; the server filter is
    a SQL predicate at the storage boundary, never post-filtering.
    """

    model_config = ConfigDict(extra="forbid")

    namespace: DocumentNamespace | None = None
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    limit: int = Field(
        default=RETRIEVAL_DEFAULT_LIMIT, ge=1, le=RETRIEVAL_MAX_LIMIT
    )
    max_excerpt_chars: int = Field(
        default=DEFAULT_MAX_EXCERPT_CHARS,
        ge=MIN_EXCERPT_CHARS,
        le=MAX_EXCERPT_CHARS,
    )


class ReconcileBody(ScopeBody):
    """Reindex-reconcile body: run the durable outbox of one scope."""

    model_config = ConfigDict(extra="forbid")


class GenerateBody(BaseModel):
    """Run-evidence generation body (07-doc-generation payload).

    ``evidence`` is the frozen ``v=1`` job-evidence document; field truth
    is :func:`mai.knowledge.generate.parse_job_evidence`. ``preview``
    validates/classifies/renders without touching the canonical store.
    """

    model_config = ConfigDict(extra="forbid")

    evidence: dict[str, Any]
    preview: bool = False


class ResearchBody(BaseModel):
    """Deep-research body: one server-scoped question over cited docs.

    ``payload`` is the frozen research-request vocabulary validated by
    :func:`mai.research.workflow.request_from_payload`. Channels the API
    process cannot truthfully provide (web search/pages, host logs),
    child runs and model synthesis are honest pre-flight rejections.
    """

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID
    payload: dict[str, Any] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Scope resolution + views
# ---------------------------------------------------------------------------


def _scope_from(body: ScopeBody) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Resolve one validated scope triple; reject ambiguous shapes."""
    if body.shared:
        if body.server_id is not None or body.app_id is not None:
            raise _validation("shared scope takes no server_id or app_id")
        return None, None
    if body.server_id is None:
        raise _validation("name exactly one of server_id or shared")
    return body.server_id, body.app_id


def _scope_from_query(
    request: Request,
) -> tuple[uuid.UUID | None, uuid.UUID | None, DocumentNamespace | None]:
    """GET scope from query params (the store re-validates identity)."""
    raw_server = request.query_params.get("server_id")
    raw_app = request.query_params.get("app_id")
    raw_shared = request.query_params.get("shared", "")
    raw_namespace = request.query_params.get("namespace")
    try:
        server_id = uuid.UUID(raw_server) if raw_server else None
        app_id = uuid.UUID(raw_app) if raw_app else None
    except ValueError:
        raise _validation("server_id and app_id must be UUIDs") from None
    shared = raw_shared.lower() in ("1", "true")
    if shared:
        if server_id is not None or app_id is not None:
            raise _validation("shared scope takes no server_id or app_id")
    elif server_id is None:
        raise _validation("name exactly one of server_id or shared")
    namespace: DocumentNamespace | None = None
    if raw_namespace:
        try:
            namespace = DocumentNamespace(raw_namespace)
        except ValueError:
            raise _validation(
                f"namespace must be one of "
                f"{sorted(ns.value for ns in DocumentNamespace)}"
            ) from None
    return server_id, app_id, namespace


def _validation(detail: str) -> BoundaryError:
    """One 422 ``validation_failed`` boundary rejection (value-free)."""
    return BoundaryError(
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        detail,
    )


def _document_view(stored: StoredDocument, *, with_text: bool) -> dict[str, object]:
    """The frozen JSON view of one stored document (provenance verbatim)."""
    view: dict[str, object] = {
        "doc_id": str(stored.doc_id),
        "server_id": (
            None if stored.server_id is None else str(stored.server_id)
        ),
        "app_id": None if stored.app_id is None else str(stored.app_id),
        "namespace": stored.namespace.value,
        "revision": stored.revision,
        "sha256": stored.sha256,
        "citation": (
            f"{stored.namespace.value}:{stored.doc_id}"
            f"#r{stored.revision}"
        ),
        "source_kind": stored.source_kind.value,
        "origin": stored.origin,
        "origin_revision": stored.origin_revision,
        "verification": stored.verification.value,
    }
    if with_text:
        view["content"] = stored.text
    return view


def _intent_view(intent: ReindexIntent) -> dict[str, object]:
    """The durable outbox view: pending work is explicit (AC2)."""
    return {
        "doc_id": str(intent.doc_id),
        "namespace": intent.namespace.value,
        "op": intent.op,
        "revision": intent.revision,
        "sha256": intent.sha256,
        "tombstone": intent.tombstone,
        "status": "pending",
    }


def _outcome_view(outcome: ReconcileOutcome) -> dict[str, object]:
    """One per-intent reconcile outcome (producer vocabulary verbatim)."""
    return {
        "doc_id": str(outcome.doc_id),
        "namespace": outcome.namespace,
        "op": outcome.op,
        "status": outcome.status.value,
        "revision": outcome.revision,
        "chunks": outcome.chunks,
        "code": None if outcome.code is None else outcome.code.value,
    }


def _store_error(request: Request, exc: DocumentStoreError) -> JSONResponse:
    """Map document-store rejections onto the C01 envelope.

    Caller validation keeps the producer's stable code in ``fields``;
    canonical-state problems (stale revision, tombstones, hash/metadata
    mismatch) are value-free ``409 conflict`` — surfaced for recovery,
    never silently repaired (C02).
    """
    if exc.code is DocumentStoreErrorCode.NOT_FOUND:
        return render_error_envelope(
            request,
            HTTP_404_NOT_FOUND,
            "not_found",
            "no canonical document stored for this document id and scope",
        )
    if exc.code in _VALIDATION_REJECTIONS:
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "document request failed validation",
            fields={"document": exc.code.value},
        )
    return render_error_envelope(
        request,
        HTTP_409_CONFLICT,
        "conflict",
        "canonical document state requires recovery or a fresher revision",
    )


# ---------------------------------------------------------------------------
# Routes (C01 prefix, envelope, identity; C07 storage boundary; C08 guards)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/docs", tags=["docs"])


@router.get(
    "",
    status_code=HTTP_200_OK,
    summary="List the scope's documents (projection view)",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Scope or pagination parameters invalid",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Document index unavailable",
        },
    },
)
async def list_documents(
    request: Request,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> Response:
    """List one scope's live documents from the SQL projection (C02 W2).

    Safe method: layer 1 only. The projection is the designed UI read
    path (the canonical file stays the authority); an unavailable
    database fails closed. Pagination is bounded (C01 rule 5).
    """
    await _require_session(request)
    server_id, app_id, namespace = _scope_from_query(request)
    services = _services(request)
    if services.retriever is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "the document index is unavailable (database not configured)",
        )
    sessions = request.app.state.session_factory
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT doc_id, server_id, app_id, namespace, revision, "
                    "sha256, source_kind, origin, origin_revision, "
                    "verification, source_path, observed_at "
                    "FROM knowledge_documents "
                    "WHERE server_id IS NOT DISTINCT FROM "
                    "CAST(:server_id AS uuid) "
                    "AND app_id IS NOT DISTINCT FROM CAST(:app_id AS uuid) "
                    "AND tombstone = false "
                    "AND (CAST(:namespace AS text) IS NULL OR namespace = :namespace) "
                    "ORDER BY observed_at DESC, doc_id "
                    "LIMIT :limit OFFSET :offset"
                ),
                {
                    "server_id": server_id,
                    "app_id": app_id,
                    "namespace": None if namespace is None else namespace.value,
                    "limit": limit,
                    "offset": offset,
                },
            )
        ).all()
    return JSONResponse(
        {
            "server_id": None if server_id is None else str(server_id),
            "app_id": None if app_id is None else str(app_id),
            "namespace": None if namespace is None else namespace.value,
            "documents": [
                {
                    "doc_id": str(row.doc_id),
                    "server_id": (
                        None if row.server_id is None else str(row.server_id)
                    ),
                    "app_id": None if row.app_id is None else str(row.app_id),
                    "namespace": row.namespace,
                    "revision": row.revision,
                    "sha256": row.sha256,
                    "citation": f"{row.namespace}:{row.doc_id}#r{row.revision}",
                    "source_kind": row.source_kind,
                    "origin": row.origin,
                    "origin_revision": row.origin_revision,
                    "verification": row.verification,
                    "source_path": row.source_path,
                    "observed_at": row.observed_at.isoformat(),
                }
                for row in rows
            ],
        }
    )


@router.post(
    "",
    status_code=HTTP_201_CREATED,
    summary="Create one canonical document (mints the document identity)",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Content, import path or scope invalid",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Canonical document service unavailable",
        },
    },
)
async def create_document(request: Request, payload: DocumentCreateBody) -> Response:
    """Mint an opaque ``doc_id`` and commit the first revision.

    Exactly one of ``content`` (authored) or ``source_path`` (explicit
    import) is required; the store refuses credential-material names,
    symlinks, directories and oversized/invalid documents before any
    effect, so a rejected import materializes nothing indexable.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    if (payload.content is None) == (payload.source_path is None):
        raise _validation("name exactly one of content or source_path")
    server_id, app_id = _scope_from(payload)
    services = _services(request)
    doc_id = uuid.uuid4()
    try:
        if payload.source_path is not None:
            stored = services.store.import_document(
                payload.source_path,
                server_id=server_id,
                app_id=app_id,
                namespace=payload.namespace,
                doc_id=doc_id,
                origin_revision=payload.origin_revision,
                verification=payload.verification,
            )
        else:
            stored = services.store.write(
                doc_id,
                payload.content or "",
                server_id=server_id,
                app_id=app_id,
                namespace=payload.namespace,
                expected_revision=0,
                source_kind=payload.source_kind(),
                origin="operator",
                origin_revision=payload.origin_revision,
                verification=payload.verification,
            )
    except DocumentStoreError as exc:
        return _store_error(request, exc)
    return JSONResponse(_document_view(stored, with_text=False), status_code=201)


@router.get(
    "/index",
    status_code=HTTP_200_OK,
    summary="Index status: durable outbox + projection state for one scope",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Scope invalid",
        },
    },
)
async def index_status(request: Request) -> Response:
    """The explicit pending/failed truth of one scope's index (AC2).

    Live outbox intents are ``pending`` — visible work, never "unknown".
    The projection side reports each live row's indexed revision and
    chunk count; when the database is unavailable the projection half
    says so instead of guessing (C08 rule 6).
    """
    await _require_session(request)
    server_id, app_id, namespace = _scope_from_query(request)
    services = _services(request)
    intents = services.store.read_intents(
        server_id=server_id, app_id=app_id, namespace=namespace
    )
    projection: object
    if services.retriever is None:
        projection = "unavailable"
    else:
        sessions = request.app.state.session_factory
        async with sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT d.doc_id, d.namespace, d.revision, d.sha256, "
                        "d.tombstone, d.observed_at, "
                        "count(c.ord) AS chunks "
                        "FROM knowledge_documents d "
                        "LEFT JOIN knowledge_doc_chunks c "
                        "ON c.document_key = d.document_key "
                        "WHERE d.server_id IS NOT DISTINCT FROM "
                        "CAST(:server_id AS uuid) "
                        "AND d.app_id IS NOT DISTINCT FROM "
                        "CAST(:app_id AS uuid) "
                        "AND (CAST(:namespace AS text) IS NULL OR d.namespace = :namespace) "
                        "GROUP BY d.document_key "
                        "ORDER BY d.doc_id"
                    ),
                    {
                        "server_id": server_id,
                        "app_id": app_id,
                        "namespace": (
                            None if namespace is None else namespace.value
                        ),
                    },
                )
            ).all()
        projection = [
            {
                "doc_id": str(row.doc_id),
                "namespace": row.namespace,
                "revision": row.revision,
                "sha256": row.sha256,
                "chunks": row.chunks,
                "tombstone": row.tombstone,
                "observed_at": row.observed_at.isoformat(),
                "status": "tombstoned" if row.tombstone else "indexed",
            }
            for row in rows
        ]
    return JSONResponse(
        {
            "server_id": None if server_id is None else str(server_id),
            "app_id": None if app_id is None else str(app_id),
            "namespace": None if namespace is None else namespace.value,
            "pending": [_intent_view(intent) for intent in intents],
            "projection": projection,
        }
    )


@router.get(
    "/{doc_id}",
    status_code=HTTP_200_OK,
    summary="Read one canonical document (hash-verified, provenance-carrying)",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical document for this id and scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Canonical state requires recovery",
        },
    },
)
async def read_document(request: Request, doc_id: uuid.UUID) -> Response:
    """Return the stored document: content plus revision/hash + provenance.

    Safe method: layer 1 only. The read is hash-verified against the
    sidecar metadata; a live tombstone or hash mismatch is a surfaced
    conflict (C02 W4/W2), never a silent answer. Naming another scope
    finds nothing (404) — scopes never leak across the storage boundary.
    """
    await _require_session(request)
    server_id, app_id, namespace = _scope_from_query(request)
    services = _services(request)
    try:
        stored = services.store.read(
            doc_id, server_id=server_id, app_id=app_id, namespace=namespace or DocumentNamespace.DOCS
        )
    except DocumentStoreError as exc:
        return _store_error(request, exc)
    return JSONResponse(_document_view(stored, with_text=True))


@router.put(
    "/{doc_id}",
    status_code=HTTP_200_OK,
    summary="Edit one canonical document (optimistic concurrency)",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical document for this id and scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale revision or canonical state conflict",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Content or revision invalid",
        },
    },
)
async def edit_document(
    request: Request, doc_id: uuid.UUID, payload: DocumentEditBody
) -> Response:
    """Replace the content at ``expected_revision`` (stale → 409 conflict).

    A stale caller is rejected BEFORE any write (C01 rule 6); a successful
    edit emits the durable reindex intent after the file+metadata pair.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    server_id, app_id = _scope_from(payload)
    services = _services(request)
    try:
        stored = services.store.write(
            doc_id,
            payload.content,
            server_id=server_id,
            app_id=app_id,
            namespace=payload.namespace,
            expected_revision=payload.expected_revision,
            source_kind=SourceKind.AUTHORED,
            origin="operator",
        )
    except DocumentStoreError as exc:
        return _store_error(request, exc)
    return JSONResponse(_document_view(stored, with_text=False))


@router.delete(
    "/{doc_id}",
    status_code=HTTP_200_OK,
    summary="Delete one canonical document (durable tombstone + reindex intent)",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical document for this id and scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale revision or canonical state conflict",
        },
    },
)
async def delete_document(
    request: Request, doc_id: uuid.UUID, payload: DocumentDeleteBody
) -> Response:
    """Revision-checked removal; the tombstone intent is durable first.

    The intent stays visible (``pending``) until the indexer acknowledges
    with a purge — delete is never reported done before the projection
    can converge (C02 W4).
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    server_id, app_id = _scope_from(payload)
    services = _services(request)
    try:
        intent = services.store.delete(
            doc_id,
            server_id=server_id,
            app_id=app_id,
            namespace=payload.namespace,
            expected_revision=payload.expected_revision,
        )
    except DocumentStoreError as exc:
        return _store_error(request, exc)
    return JSONResponse({"deleted": True, "intent": _intent_view(intent)})


@router.post(
    "/retrieve",
    status_code=HTTP_200_OK,
    summary="Hybrid search over one scope (cited, provenance-preserving)",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query or scope invalid",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Document index unavailable",
        },
    },
)
async def retrieve_documents(request: Request, payload: RetrievalBody) -> Response:
    """Answer one bounded hybrid query (pgvector cosine + FTS, RRF-fused).

    Every excerpt carries its full citation tuple (doc id, revision,
    hash, chunk offset), so truncated text stays traceable (C07 rule 4).
    ``status: "unavailable"`` names the failed seam explicitly (index vs
    embedding vs dimension contract) — never an empty-success from a
    failed query (AC2). The server filter is SQL at the storage boundary
    (C07 rule 2).
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    server_id, app_id = _scope_from(payload)
    _, retriever = _embedding_services(request)
    result = await retriever.retrieve(
        server_id=server_id,
        app_id=app_id,
        namespace=payload.namespace,
        query=payload.query,
        limit=payload.limit,
        max_excerpt_chars=payload.max_excerpt_chars,
    )
    if result.status is RetrievalStatus.UNAVAILABLE:
        # An honest outage answer: 200 body names the failed seam; the
        # caller can distinguish "ran, zero matches" (ok + empty) from
        # "index down" (unavailable + code) — never conflated.
        return JSONResponse(result_document(result))
    return JSONResponse(result_document(result))


@router.post(
    "/index/reconcile",
    status_code=HTTP_200_OK,
    summary="Reconcile the scope's reindex outbox (reindex now)",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Scope invalid",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Document index unavailable",
        },
    },
)
async def reconcile_index(request: Request, payload: ReconcileBody) -> Response:
    """Run every live intent of one scope through the reviewed indexer.

    Per-intent outcomes are reported verbatim; an embedding/DB outage
    leaves the intent ``pending`` with the producer's stable code —
    never a fabricated ``indexed`` (the outbox converges on the next
    reconcile).
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    server_id, app_id = _scope_from(payload)
    services = _services(request)
    if services.indexer is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "the document index is unavailable (database or embeddings "
            "endpoint not configured)",
        )
    outcomes = await services.indexer.reconcile_scope(
        server_id=server_id, app_id=app_id
    )
    return JSONResponse(
        {
            "server_id": None if server_id is None else str(server_id),
            "app_id": None if app_id is None else str(app_id),
            "outcomes": [_outcome_view(outcome) for outcome in outcomes],
        }
    )


@router.post(
    "/generate",
    status_code=HTTP_200_OK,
    summary="Generate a canonical runbook/incident from settled run evidence",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Run evidence invalid",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Canonical state conflict (tombstone/hash)",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Canonical document service unavailable",
        },
    },
)
async def generate_document(request: Request, payload: GenerateBody) -> Response:
    """Preview or commit one generated document (C07 rule 5 boundary).

    Classification comes from run truth, never prose: settled
    ``succeeded`` → runbook (``verified`` only when host-verified),
    settled ``failed`` → incident record; anything unsettled is rejected
    before any store touch. ``preview`` renders without writing; a commit
    writes revision-checked through the canonical store and honestly
    reports the reindex outcome (``null`` when no indexer is wired).
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    services = _services(request)
    try:
        evidence = parse_job_evidence(payload.evidence)
        if payload.preview:
            classification, namespace, verification = classify(evidence)
            return JSONResponse(
                {
                    "preview": True,
                    "namespace": namespace.value,
                    "classification": classification.value,
                    "verification": verification.value,
                    "content": render_document(
                        evidence, classification, verification
                    ),
                }
            )
        reconcile = None
        if services.indexer is not None:
            indexer = services.indexer

            async def reconcile() -> tuple[object, ...]:
                return tuple(
                    await indexer.reconcile_scope(
                        server_id=evidence.server_id,
                        app_id=evidence.app_id,
                    )
                )

        result = await DocGenerator(
            services.store, reconcile=reconcile
        ).generate(payload.evidence)
    except (DocGenerationError, DocumentStoreError) as exc:
        if isinstance(exc, DocumentStoreError):
            return _store_error(request, exc)
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "run evidence failed validation",
            fields={"evidence": exc.code.value},
        )
    return JSONResponse(
        {
            "preview": False,
            "doc": _document_view(result.doc, with_text=False),
            "classification": result.classification.value,
            "verification": result.verification.value,
            "reindex": (
                None
                if result.reindex is None
                else [
                    _outcome_view(outcome)
                    for outcome in result.reindex
                    if isinstance(outcome, ReconcileOutcome)
                ]
            ),
        }
    )


@router.post(
    "/research",
    status_code=HTTP_200_OK,
    summary="Run one bounded deep-research pass over the server's cited docs",
    responses={
        HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No enrolled profile for this server",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Research payload invalid",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Research channel or dependency unavailable",
        },
    },
)
async def research_documents(request: Request, payload: ResearchBody) -> Response:
    """Execute one bounded research run and return the settled report.

    The workflow composes, through the ONE dispatcher boundary, the
    current pinned server context and scoped docs retrieval; every
    excerpt becomes a cited source (C07 rule 4) and a failed run promotes
    exactly one canonical incident (C07 rule 5). Channels the API
    process cannot truthfully provide — web search/pages, host logs,
    child runs, model synthesis — are honest pre-flight rejections, so a
    settled report never contains silently missing evidence.
    """
    _, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    services = _services(request)
    if services.workflow is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "the research channel is unavailable (document index, grant "
            "configuration or profile service not configured)",
        )
    profiles = getattr(request.app.state, "profiles", None)
    if profiles is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "the canonical profile service is unavailable",
        )
    try:
        research_request = request_from_payload(payload.payload)
    except ResearchError as exc:
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "research payload failed validation",
            fields={"payload": exc.code.value},
        )
    if research_request.synthesize:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "model synthesis is not available on this surface; synthesis "
            "runs in the worker-owned model channel",
        )
    if research_request.children:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "child research runs are not available on this surface; "
            "child execution is worker-owned",
        )
    if research_request.search_queries or research_request.page_reads or research_request.logs:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "web search, page and log evidence channels are not available "
            "on this surface",
        )
    try:
        stored = profiles.read(payload.server_id)
    except Exception:
        raise BoundaryError(
            HTTP_404_NOT_FOUND,
            "not_found",
            "no enrolled profile stored for this server id",
        ) from None
    profile = parse_server_profile_text(stored.text)
    context = ExecutionContext(
        principal=hash_token(token),
        server_id=payload.server_id,
        run_id=uuid.uuid4(),
        parent_scope=None,
        profile_revision=str(stored.revision),
        prompt_revision=str(profile.prompt_revision),
        credential_references=(),
        allowed_capabilities=(RESEARCH_CAPABILITY,),
        mode=PolicyMode.ASK,
    )
    report = await services.workflow.run(context=context, payload=payload.payload)
    return JSONResponse(report_document(report))


def register_documents(app: FastAPI) -> None:
    """Register the documents surface under the shared boundary (api-wiring)."""
    app.include_router(router)
