# mai-07-chunk-index — evidence index

**Card:** P07 · service `knowledge` · risk `high` · paired review `r-07-chunk-index`
**Slice:** `backend/src/mai/knowledge/index.py` (new),
`backend/tests/test_index.py` (new) — allowlist-complete; no tracked file
modified, no dependency added (`uv.lock` untouched)
**Worktree/branch:** `/home/megastruktur/orca/workspaces/machina/mai-07-chunk-index`
· `megastruktur/mai-07-chunk-index`
**Base HEAD:** `6cbd391` — P07 tip ("integrate reviewed slice 07-vector-schema")
**Status:** COMPLETE — 43 focused tests green (first-run failures preserved in
`01-commands.txt`, no retry-until-green), 181 passed across the five
producer/consumer sibling suites, production-CLI transcript on a disposable
fixture (indexed → update → delete → pending-negative → structured
rejections), fixture container and all disposable files destroyed, committed clean.

## Result summary

Section-aware Markdown chunking + pinned-gateway embedding generation +
idempotent pgvector/FTS reindexing consumer for the `07-doc-store` durable
intent outbox, per contracts.md **C07** rules 2/3 and the knowledge-packaging
ADR §4.2 dimension contract / §4.3 rebuild contract:

- **Chunking** — flat ATX-heading sections (preamble = own section), greedy
  block packing under a 4000-char cap with line/character hard split, heading
  prefix counted against the cap, deterministic chunk identity
  `(revision-specific parent row, ord)` stable across retries; credential
  fences excluded *structurally* by declared info string (C03 rule 6
  posture — no content scanning).
- **Embedding** — the pinned `openai` SDK surface (`POST {base_url}/v1/
  embeddings`, never `/chat/completions`) over the validated
  `06-model-config` `ModelConnection`; `credential_ref` resolves through the
  `03-credentials` `.env` resolver at request time; model/dims are frozen
  constants (`text-embedding-3-small`, 1536) — wrong-length responses are
  refused before any write (`dimension_mismatch`); 128-text batches preserve
  order.
- **Reindex consumer** — per-scope outbox sweep (`ScopeIndexer`): upsert =
  hash-check → chunk → **embed first** → single-transaction swap (cascade
  deletes stale chunks; insert row + chunks carrying the frozen dimension
  tuple) → purge intent (C02 W4 ack after confirm); delete = two-phase
  tombstone + chunk removal, then row purge + ack. Every phase idempotent:
  duplicate deliveries → `unchanged`, ack crashes and phase-1 crash windows
  re-converge, unknown sources → `absent`. Unavailable embedding / missing
  source / hash mismatch → `pending` outcome, intent stays live, projection
  untouched — visible pending index, never empty-success.

## Index

| File | Content |
|---|---|
| `README.md` | This index: result summary, file index, acceptance mapping, provenance/limitations |
| `01-commands.txt` | Exact commands + outcomes; fixture lifecycle; production-CLI transcript (write/reconcile/psql state/update/delete/pending-negative/structured rejections); acceptance→test map; binding design decisions; first-run failures preserved; limitations |
| `02-focused-tests.txt` | Receipts: `pytest tests/test_index.py -v` — **43 passed**; sibling suites (`test_documents`, `test_knowledge_schema`, `test_knowledge_dependencies`, `test_models`, `test_env_resolver`) — **181 passed**; no-fixture run — 28 passed / 15 skipped |
| `03-negative-controls.txt` | Negative-control selection (`-k …`) — **20 passed**: credential-fence exclusion (7 info strings) + keep-side (4), dimension refusal, broken endpoint, credential-ref resolution failures, unknown-source delete, pending controls (unavailable embedding, missing source, hash mismatch, failed-reembed keeps old revision searchable) |
| `04-cleanup-proof.txt` | Pre-cleanup inventory, removal commands, post-cleanup absence proof (container, tmp root, stub process), in-repo scaffold check |
| `05-source-tree.txt` | Source/tree/build identity (pre-commit): base `6cbd391`, branch, allowlist-only untracked diff, empty lock/manifest diff, toolchain + locked lane versions |
| `06-post-commit-receipt.txt` | Post-commit: SHA, parent, diffstat, clean-tree proof (written after commit) |

## Acceptance criteria → evidence

| AC | Evidence |
|---|---|
| 1. Stable revision/offset chunks with embedding model identity; retries do not duplicate current source chunks | Chunk stability/order (`TestChunking`); dimension tuple + `vector_dims()` on real rows, `source_path` == on-disk canonical path (`test_upsert_indexes_chunks_with_dimension_tuple_and_fts`); duplicate delivery → `unchanged`, 1 doc/1 chunk row (`test_confirmed_retry_…`); commit→ack crash re-converges without duplicates (`test_crash_between_commit_and_purge_reconverges`); frozen tuple on every row (`test_model_identity_tuple_…`) |
| 2. Update/delete removes stale retrieval results; unavailable embedding leaves visible pending index, not empty-success | Real FTS staleness (`test_update_swaps_revision_and_removes_stale_results`); delete rows+chunks gone + both crash windows (`TestReconcileDelete`); pending semantics on real DB (`TestReconcilePending`) and in the CLI transcript (dead endpoint → `status: pending`, `code: embedding_unavailable`, summary `pending: 1`, intent still listed, 0 projection rows, rc=0) |
| Feature-gate scenario (writer half, KB-FILTER): per-server result sets + deletion propagation | `test_scope_isolation_server_app_shared_at_storage_boundary` (same doc identity under two servers + shared; per-scope corpora; server A never matches shared-only term); update/delete propagation covered above. Assembled kill-mid-batch journey with the live engine: owner `07-verify` |

## Provenance and limitations

- All commands ran from `backend/` in the feature-shared worktree
  `mai-07-chunk-index` (branch `megastruktur/mai-07-chunk-index`, base
  `6cbd391`); receipts recorded pre-commit on 2026-10-16 (~12:00–12:25Z,
  files 01–05) and post-commit (`06-post-commit-receipt.txt`).
- Fixtures: disposable Docker container `mai-fxt-07-chunk-index`
  (`pgvector/pgvector:pg16`, own port 55433; destroyed — `04-cleanup-proof.txt`);
  pytest-managed `tmp_path` roots only; CLI transcript root
  `/tmp/mai-fxt-07-chunk-index-cli` (destroyed); one throwaway stub
  embeddings server (script destroyed, process killed; served constant
  1536-dim vectors, no auth). Only credential-shaped string in the whole
  evidence set is the offline `EMBEDDING_TOKEN=fxt-value` in pytest tmp
  fixtures.
- No dependency added: the module consumes the `07-dependencies` lane
  (`openai`, `httpx2`, `pgvector`, SQLAlchemy) exactly as pinned by
  `test_knowledge_dependencies.py`; `uv.lock` untouched.
- Mocks: only at the HTTP boundary (httpx2 `MockTransport` + one local stub
  server) — every storage, idempotency, staleness and isolation claim runs
  against the real disposable PostgreSQL with the real Alembic chain, and
  the CLI subprocess test runs the real entrypoint in a fresh OS process.
- Limitations: the `05-steps` engine wrap and the real-provider embedding
  round trip (KB-E1) are owned by `07-verify`/`00-verify`; this slice proves
  the reconciler's restart/crash convergence properties at its own boundary
  and pins the gateway surface offline (see `01-commands.txt` §Limitations).
