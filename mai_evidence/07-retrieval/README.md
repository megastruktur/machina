# mai-07-retrieval — evidence index

**Card:** P07 · service `knowledge` · risk `high` · paired review `r-07-retrieval`
**Slice:** `backend/src/mai/knowledge/retrieve.py` (new, 956 lines),
`backend/tests/test_retrieval.py` (new, 1160 lines) — allowlist-complete;
no tracked file modified, no dependency added (`uv.lock` untouched)
**Worktree/branch:** `/home/megastruktur/orca/workspaces/machina/mai-07-retrieval`
· `megastruktur/mai-07-retrieval`
**Base HEAD:** `1ab545c` — P07 tip ("integrate reviewed slice 07-chunk-index")
**Status:** COMPLETE — 46 focused tests green (first-run failures preserved
in `01-commands.txt`), 148 + 147 passed across the producer/consumer
sibling suites, production-CLI KB-FILTER transcript on a disposable
fixture (conflicting docs per server, explicit shared scope, citation
hash resolution, unavailable-index distinct from zero), fixture container,
probe database, loopback embedding server and all disposable files
destroyed, committed clean.

## Result summary

Server-filtered hybrid document retrieval exposed as the `docs.retrieve`
capability, per contracts.md **C07** rules 2/3/4 and the
knowledge-packaging ADR §4.2 dimension contract:

- **Storage-boundary scope filter (C07 rule 2)** — the server filter is
  a SQL predicate on the projection (`server_id IS NOT DISTINCT FROM
  :server_id AND app_id IS NOT DISTINCT FROM :app_id AND tombstone =
  false`), never post-filtering; the shared namespace (`server_id IS
  NULL`) answers only to an explicit payload scope. The payload carries
  no target field: `scope="server"` resolves to the
  `ExecutionContext.server_id` (C03 rule 2), and the bridge schema gate
  denies a smuggled `server_id` before any dispatch.
- **Hybrid ranking** — bounded candidate pools from pgvector cosine
  (HNSW `vector_cosine_ops`) and PostgreSQL FTS (`ts_rank` over the
  stored generated tsvector, `simple` config), fused by reciprocal-rank
  fusion (k=60); per-excerpt character budget and result cap bound the
  answer (excerpts, never the corpus).
- **Provenance-preserving citations (C07 rule 4)** — every excerpt
  carries the full citation tuple (`document_key`, `doc_id`, `namespace`,
  `revision`, `sha256`, `ord`, rendered `docs:<id>#r<rev>:<ord>`) plus
  `source_kind`/`origin`/`origin_revision`/`verification` and the
  projection-owned `observed_at`; length-truncated excerpts keep every
  identifier.
- **Dimension contract (rule 3)** — the query embedding flows through
  the same pinned seam as the indexer; a wrong-length response is
  refused (`dimension_mismatch`), never compared against the index.
- **Honest availability (AC2)** — storage failure (`index_unavailable`)
  and embedding-seam failure (`embedding_unavailable` /
  `dimension_mismatch`) are distinct `status="unavailable"` outcomes with
  value-free error type names; a query that ran and matched nothing is
  `status="ok"` with an empty list. Never an empty-success from a failed
  query.
- **Capability/tool integration (C03)** — `effect_class="read"` (no
  approval decision; every grant/scope/policy gate still enforced),
  `execution_environments=["controller"]`, `required_grants=
  ["docs.retrieve"]`; proven against the real registry, dispatcher,
  policy and `06-tool-bridge` (exposure follows `allowed_capabilities`;
  grant-scope and deny-list denials keep the executor uninvoked).

## Files

- `01-commands.txt` — exact commands and outcomes: fixture lifecycle,
  focused + sibling test runs, first-run failures, full production-CLI
  transcript (doc-store write → index reconcile → retrieve queries per
  scope → unavailable-index and rejection negatives).
- `02-focused-tests.txt` — 46 tests mapped to acceptance criteria,
  hybrid-ranking proofs, capability/tool integration, payload matrix.
- `03-negative-controls.txt` — eight real-boundary negative controls
  (cross-server leakage, shared-scope explicitness, smuggled target,
  grant scope, policy deny, unavailable-vs-zero, payload matrix,
  stale-after-update/delete) + the out-of-scope pre-existing
  `test_dispatch.py` finding.
- `04-cleanup-proof.txt` — destruction of the fixture container, probe
  database, loopback embedding server and `/tmp` files.
- `05-source-tree.txt` — tree, base HEAD, build/toolchain versions,
  allowlist conformance, verification table.

## Limitations

- Embedding round trips use deterministic local fixtures (offline
  discipline); a live-gateway round trip is the feature gate's
  assembled-journey concern (`07-verify`).
- The vector channel is k-NN: a non-matching query over a non-empty
  scope returns the closest chunks of that scope (rank/score convey
  weakness); zero results is the empty-scope state. No arbitrary
  relevance threshold was invented.
- `tests/test_dispatch.py` shows 5 pre-existing fixture errors on the
  base tree (reproduced with this slice absent); recorded for the
  coordinator, outside this card's allowlist.
