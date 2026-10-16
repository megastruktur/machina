"""document metadata + selected vector storage schema (card 07-vector-schema)

Schema for contracts.md **C07** (documents, retrieval and memory) rules 1–3
and the knowledge-packaging ADR (`5d6e0a1`) §4.2 dimension contract / §4.3
rebuild contract:

- ``knowledge_documents`` — the SQL projection of the C07 rule-1 source doc
  record, reconciled from the durable reindex intents emitted by the
  canonical file store (``mai.knowledge.documents``, card 07-doc-store).
  Scope is explicit: ``server_id`` NULL denotes the shared namespace,
  optional ``app_id`` (an app scope requires a server, mirroring the file
  store), ``namespace`` pins the frozen docs/runbooks vocabulary. The row
  carries the revision/hash pair, provenance (``source_kind``, ``origin``,
  ``origin_revision``), ``verification`` (outcomes vs hypotheses stay
  separated at the storage boundary, C07 rule 5) and the canonical-store
  ``source_path``. Timestamps are projection-owned (the file store is the
  revision/hash half and never writes clocks). ``tombstone`` /
  ``tombstoned_at`` make a delete durable first (C02 W4): the tombstone row
  stays until the indexer purges it, and purge removes the row.
  Uniqueness of a source revision is a real constraint
  (``uq_knowledge_documents_source_revision``, NULLS NOT DISTINCT so the
  shared scope participates fully) — re-applying one reindex intent is
  idempotent and a stale double-apply is an integrity violation, never a
  silent second row. A partial unique index
  (``uq_knowledge_documents_live``) allows exactly one live (non-tombstone)
  revision per source identity.
- ``knowledge_doc_chunks`` — the vector-bearing chunk store (C07 rules 2/3).
  The primary key ``(document_key, ord)`` encodes the rule-2 chunk identity
  "derived from source revision/offset": the parent row is
  revision-specific, so an updated or deleted source removes its stale
  chunks through the ``ON DELETE CASCADE`` deletion relation — the storage
  half of the §4.3 rebuild contract (delete → re-chunk → re-embed →
  re-index; all vector data is derived, never canonical). The server filter
  is applied at this storage boundary by joining the scoped parent row —
  not prompt-only. ``search_vector`` is a stored generated tsvector
  (``simple`` config, language-neutral: no stemming assumptions for
  operator docs) with a GIN index — PostgreSQL-native FTS, no extra
  extension. The embedding column is pinned ``vector(1536)`` with an HNSW
  ``vector_cosine_ops`` index.
- Dimension contract tuple (ADR §4.2, binding): every vector-bearing table
  carries ``(embedding_provider, embedding_model, embedding_dimensions,
  distance_metric)``; CHECK constraints pin the v1 values
  ``("openai-compatible", "text-embedding-3-small", 1536, "cosine")`` so a
  model/dims switch can never be silently mixed into one index — it is a
  migration/rebuild event, and a consumer finding configured model/dims ≠
  index metadata refuses to query (rule 3). Wrong-length vectors are
  rejected twice: by the pinned column type and (in the ingest path) by the
  ``vector_dims()`` check.

The ``vector`` extension is created with IF NOT EXISTS: the pgvector-enabled
Postgres image (platform/Compose cards, pgvector ≥ 0.8.0 per ADR §4.1) is
the delivery mechanism, and re-upgrades stay idempotent. Downgrade
intentionally does NOT drop the extension: it is database-level
infrastructure that may have pre-existed this chain, and table round-trip
is the migration contract.

One linear chain, one database: vectors live in the controller's own
PostgreSQL (ADR §4.1 — a dedicated vector-database service is rejected); no
second database, no second store.

Chain position: fifth revision of the single linear migration chain
(exclusive ``migration-chain`` resource); ``down_revision`` is ``0004`` —
this migration serializes strictly after the P05 jobs schema. DBOS system
tables stay engine-owned and out of this chain (``env.py``).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen dimension contract (knowledge-packaging ADR §4.2; contracts.md C07
# rule 3; pinned by backend/tests/test_knowledge_dependencies.py and
# test_knowledge_schema.py — a drift fails loudly at both layers).
EMBEDDING_PROVIDER = "openai-compatible"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
DISTANCE_METRIC = "cosine"

# Frozen source-record vocabularies, mirrored from mai.knowledge.documents
# (card 07-doc-store; the file half of the same C07 rule-1 record).
_NAMESPACES = ("docs", "runbooks")
_SOURCE_KINDS = ("authored", "imported", "generated")
_VERIFICATION_STATES = ("unverified", "verified", "hypothesis")

_NAMESPACES_SQL = ", ".join(f"'{value}'" for value in _NAMESPACES)
_SOURCE_KINDS_SQL = ", ".join(f"'{value}'" for value in _SOURCE_KINDS)
_VERIFICATION_SQL = ", ".join(f"'{value}'" for value in _VERIFICATION_STATES)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_documents",
        sa.Column(
            "document_key",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            comment="Internal row identity; the chunk-table foreign key "
            "target. Source identity lives in the scope + revision "
            "constraints below.",
        ),
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Opaque document identity (C01 rule 1), shared with the "
            "canonical file store.",
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Owning server; NULL is the explicit shared namespace "
            "(C07 rule 1). Storage-boundary scope filter key (C07 rule 2).",
        ),
        sa.Column(
            "app_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Optional application scope; requires a server "
            "(mirrors the canonical store's app-scope rule).",
        ),
        sa.Column(
            "namespace",
            sa.Text(),
            nullable=False,
            comment="Frozen docs/runbooks vocabulary (C02 rule 2 layout "
            "half owned by the knowledge lane).",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            comment="Integer content revision from the canonical store "
            "(starts at 1; the file store owns the revision/hash half).",
        ),
        sa.Column(
            "sha256",
            sa.Text(),
            nullable=False,
            comment="SHA-256 of the canonical revision content "
            "(hex, 64 chars).",
        ),
        sa.Column(
            "source_kind",
            sa.Text(),
            nullable=False,
            comment="authored / imported / generated provenance class "
            "(C07 rule 1).",
        ),
        sa.Column(
            "origin",
            sa.Text(),
            nullable=False,
            comment="Opaque provenance string (operator name, import "
            "source path, …).",
        ),
        sa.Column(
            "origin_revision",
            sa.Text(),
            nullable=True,
            comment="Optional version of the origin document.",
        ),
        sa.Column(
            "verification",
            sa.Text(),
            nullable=False,
            comment="unverified / verified / hypothesis — outcomes stay "
            "separated from hypotheses at the storage boundary (C07 rule 5).",
        ),
        sa.Column(
            "source_path",
            sa.Text(),
            nullable=False,
            comment="Canonical-store relative path of the source revision "
            "(C07 rule 1 'path').",
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="Projection-owned observed/generated time (C07 rule 1); "
            "the file store never writes clocks.",
        ),
        sa.Column(
            "tombstone",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Durable delete marker (C02 W4): made durable first, "
            "purged only after the projection confirms.",
        ),
        sa.Column(
            "tombstoned_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set exactly when tombstone becomes true.",
        ),
        sa.CheckConstraint(
            "namespace IN (%s)" % _NAMESPACES_SQL,
            name="ck_knowledge_documents_namespace",
        ),
        sa.CheckConstraint(
            "revision >= 1", name="ck_knowledge_documents_revision"
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_knowledge_documents_sha256"
        ),
        sa.CheckConstraint(
            "source_kind IN (%s)" % _SOURCE_KINDS_SQL,
            name="ck_knowledge_documents_source_kind",
        ),
        sa.CheckConstraint(
            "char_length(origin) BETWEEN 1 AND 512",
            name="ck_knowledge_documents_origin",
        ),
        sa.CheckConstraint(
            "origin_revision IS NULL OR char_length(origin_revision)"
            " BETWEEN 1 AND 128",
            name="ck_knowledge_documents_origin_revision",
        ),
        sa.CheckConstraint(
            "verification IN (%s)" % _VERIFICATION_SQL,
            name="ck_knowledge_documents_verification",
        ),
        sa.CheckConstraint(
            "char_length(source_path) BETWEEN 1 AND 1024",
            name="ck_knowledge_documents_source_path",
        ),
        sa.CheckConstraint(
            "app_id IS NULL OR server_id IS NOT NULL",
            name="ck_knowledge_documents_app_requires_server",
        ),
        sa.CheckConstraint(
            "tombstone = (tombstoned_at IS NOT NULL)",
            name="ck_knowledge_documents_tombstone_pair",
        ),
        # Source revision uniqueness (NULLS NOT DISTINCT: the shared scope
        # participates fully). Re-applying one reindex intent is idempotent;
        # a double-apply is an integrity violation.
        sa.UniqueConstraint(
            "server_id",
            "app_id",
            "namespace",
            "doc_id",
            "revision",
            name="uq_knowledge_documents_source_revision",
            postgresql_nulls_not_distinct=True,
        ),
    )
    # At most one live (non-tombstone) revision per source identity; also
    # the storage-boundary lookup index for the server/shared scope filter.
    op.create_index(
        "uq_knowledge_documents_live",
        "knowledge_documents",
        ["server_id", "app_id", "namespace", "doc_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("tombstone = false"),
    )

    op.create_table(
        "knowledge_doc_chunks",
        sa.Column(
            "document_key",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Parent source revision row; cascade delete is the "
            "deletion relation behind the §4.3 rebuild contract.",
        ),
        sa.Column(
            "ord",
            sa.Integer(),
            nullable=False,
            comment="Chunk offset within the source revision — chunk IDs "
            "derive from source revision/offset (C07 rule 2).",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="Chunk text; credentials were excluded before chunking "
            "by the chunk-index consumer (C07 rule 1).",
        ),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=False,
            comment="Stored generated tsvector (simple config — "
            "language-neutral FTS, no stemming assumption).",
        ),
        sa.Column(
            "embedding",
            Vector(EMBEDDING_DIMENSIONS),
            nullable=False,
            comment="Pinned vector(1536); wrong lengths are rejected by the "
            "column type and the ingest-path vector_dims() check "
            "(ADR §4.2).",
        ),
        sa.Column(
            "embedding_provider",
            sa.Text(),
            nullable=False,
            comment="Dimension contract tuple, ADR §4.2 — pinned per row "
            "so a model switch can never mix into one index.",
        ),
        sa.Column(
            "embedding_model",
            sa.Text(),
            nullable=False,
            comment="Dimension contract tuple, ADR §4.2.",
        ),
        sa.Column(
            "embedding_dimensions",
            sa.Integer(),
            nullable=False,
            comment="Dimension contract tuple, ADR §4.2; must equal the "
            "pinned column dimension.",
        ),
        sa.Column(
            "distance_metric",
            sa.Text(),
            nullable=False,
            comment="Dimension contract tuple, ADR §4.2; matches the HNSW "
            "operator class.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="UTC chunk record time (C01 rule 5).",
        ),
        sa.PrimaryKeyConstraint(
            "document_key", "ord", name="pk_knowledge_doc_chunks"
        ),
        sa.ForeignKeyConstraint(
            ["document_key"],
            ["knowledge_documents.document_key"],
            name="fk_knowledge_doc_chunks_document",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "ord >= 0", name="ck_knowledge_doc_chunks_ord"
        ),
        sa.CheckConstraint(
            "char_length(content) > 0", name="ck_knowledge_doc_chunks_content"
        ),
        sa.CheckConstraint(
            "embedding_provider = '%s'" % EMBEDDING_PROVIDER,
            name="ck_knowledge_doc_chunks_provider",
        ),
        sa.CheckConstraint(
            "embedding_model = '%s'" % EMBEDDING_MODEL,
            name="ck_knowledge_doc_chunks_model",
        ),
        sa.CheckConstraint(
            "embedding_dimensions = %d" % EMBEDDING_DIMENSIONS,
            name="ck_knowledge_doc_chunks_dimensions",
        ),
        sa.CheckConstraint(
            "distance_metric = '%s'" % DISTANCE_METRIC,
            name="ck_knowledge_doc_chunks_metric",
        ),
    )
    op.create_index(
        "ix_knowledge_doc_chunks_embedding_hnsw",
        "knowledge_doc_chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_knowledge_doc_chunks_search_vector",
        "knowledge_doc_chunks",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    # The vector extension is database-level infrastructure and is
    # deliberately not dropped here (see module docstring).
    op.drop_index(
        "ix_knowledge_doc_chunks_search_vector",
        table_name="knowledge_doc_chunks",
    )
    op.drop_index(
        "ix_knowledge_doc_chunks_embedding_hnsw",
        table_name="knowledge_doc_chunks",
    )
    op.drop_table("knowledge_doc_chunks")
    op.drop_index("uq_knowledge_documents_live", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
