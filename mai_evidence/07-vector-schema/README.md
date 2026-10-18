# mai-07-vector-schema — evidence

Date: 2026-10-16 · card: `07-vector-schema` (P07, service `database`) · risk: medium

## Delivered slice

- `backend/migrations/versions/0005_knowledge.py` — fifth revision of the single
  linear Alembic chain (`down_revision="0004"`, exclusive migration-chain resource):
  - `knowledge_documents` — SQL projection of the C07 rule-1 source doc record
    (scope: `server_id NULL` = explicit shared namespace, optional `app_id`
    requiring a server, frozen `docs`/`runbooks` namespace; revision + SHA-256;
    `source_kind`/`origin`/`origin_revision`; `verification`;
    projection-owned `observed_at`; `source_path`; durable `tombstone` +
    `tombstoned_at` pair, C02 W4).
    `uq_knowledge_documents_source_revision` (NULLS NOT DISTINCT) makes one
    source revision idempotent to re-apply; the partial unique index
    `uq_knowledge_documents_live` allows exactly one live revision per source.
  - `knowledge_doc_chunks` — vector-bearing chunk store (C07 rules 2/3):
    PK `(document_key, ord)` = chunk identity derived from source
    revision/offset; `ON DELETE CASCADE` to the revision row is the deletion
    relation behind the ADR §4.3 rebuild (delete → re-chunk → re-embed →
    re-index; all vector data is derived); pinned `vector(1536)` column with
    HNSW `vector_cosine_ops`; stored generated `tsvector` (`simple` config,
    language-neutral) + GIN; the dimension contract tuple
    `(embedding_provider, embedding_model, embedding_dimensions,
    distance_metric)` pinned by CHECKs to the frozen v1 values
    `("openai-compatible", "text-embedding-3-small", 1536, "cosine")`.
  - `CREATE EXTENSION IF NOT EXISTS vector` (pgvector ≥ 0.8.0 delivered by the
    platform image; downgrade deliberately does not drop database-level
    infrastructure).
- `backend/tests/test_knowledge_schema.py` — 37 focused tests, all against a
  real disposable pgvector fixture (`MAI_TEST_DATABASE_URL`), no mocks.
- `backend/tests/test_job_schema.py` — **chain-tip reconciliation only**
  (see below): 15 insertions / 4 deletions.

## Acceptance mapping

1. **Selected embedding/model/dimensions and explicit server/shared scope
   encoded in schema** — tuple columns + CHECK pins + `vector(1536)` +
   HNSW `vector_cosine_ops`; shared scope = NULL `server_id` with NULLS NOT
   DISTINCT uniqueness; app scope requires a server. Proven by
   `TestDimensionContract`, `TestServerSharedScope` (incl. storage-boundary
   join filter: a server-scoped query sees only that server's chunks).
2. **Source revision uniqueness, tombstones and deletion relations support
   rebuild; no surprise second database** — real `IntegrityError` on
   duplicate source revision; one live revision enforced by the partial
   unique index; tombstone pair CHECK; chunk cascade + successful re-insert
   after purge (rebuild at the storage layer); exact public table set pinned
   (13 tables), no `dbos`/`hindsight` schema, `vector` extension installed in
   the same database the app tables live in.

## Chain-tip reconciliation (out-of-allowlist, minimal, required)

The exclusive `migration-chain` resource makes this card the chain owner.
`backend/tests/test_job_schema.py` (card 05) pinned the tip at `0004`; four
assertions/fixtures would go stale the moment `0005` lands:

1. `REVISION_IDS` extended with `"0005"`.
2. `EXPECTED_APP_TABLES` extended with the two knowledge tables (the exact
   public-set pin at the tip).
3. `test_downgrade_upgrades_cleanly` final head assertion `("0004",)` →
   `(REVISION_IDS[-1],)`.
4. `_drop_app_tables` extended with the knowledge tables so the zero-to-head
   fixture rebuilds the full app set (order-independent combined runs;
   verified both orders, 149 passed each).

No 05 behavior assertions were changed. Pre-existing stale tip pins in
`test_approvals.py`/`test_profile_projection.py` (`0003`) and their
module-local drop lists were already erroring on a shared live fixture
before this card (recorded in `01-commands.txt`); they belong to their
owners and were left untouched.

## Verification summary

- Migration entrypoint: real `alembic upgrade head` / `downgrade 0004` /
  re-upgrade against the fixture; `\d` inspection recorded.
- Focused: 37/37 new tests; 112/112 reconciled 05 suite; 68/68 adjacent
  07 suites (doc-store, knowledge dependencies); 149/149 combined in both
  module orders. Full output: `02-focused-tests.txt`.
- Negative controls: 22 real rejection boundaries (wrong vector lengths
  1535/1537/1/3072, all four tuple pins, duplicate source revision, two live
  revisions, app-without-server, both tombstone-pair mismatches, unknown
  namespace/source_kind/verification, four malformed sha256 shapes,
  cross-server storage-boundary filter). Full output:
  `03-negative-controls.txt`.
- Fixture: `pgvector/pgvector:pg16` (PostgreSQL 16.14, pgvector 0.8.5 —
  ADR floor ≥ 0.8.0), loopback-only port, removed after the runs;
  `04-cleanup-proof.txt`.

## Limitations

- Embedding *ingest-path* `vector_dims()` double-check and the consumer
  refuse-to-query rule are write-path/application concerns owned by
  `07-chunk-index`/`07-retrieval`; this card enforces the dimension contract
  at the storage boundary (column type + CHECK pins) and pins the tuple.
- FTS uses the `simple` text search config (language-neutral, no stemming);
  a language-specific config is a later deliberate contract.
- The feature-gate scenario (migrate real store + round-trip with server
  filters, revision constraints, dimension mismatch failures) is owned by
  `07-verify`; this card's tests map to its fragments.
- Hindsight banks / SearXNG are out of scope per the allowlist.
