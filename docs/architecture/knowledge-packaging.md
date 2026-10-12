# Architecture decision: knowledge packaging (Hindsight, SearXNG, embeddings)

- Resource: `architecture-decision:00-knowledge-packaging` · Phase P00 · card `mai-00-knowledge-packaging`
- Status: **Proposed** — accepted only at the P00 phase gate; this document is the phase output, not a shipped product.
- Basis: `IDEA.md` §2 (services table rows "Search" and "Operational memory"), §8 (OpenAI-compatible model access), §10 (canonical documents, chunking, vector retrieval), §11 (Memory), §12 (Search).
- Contract mapping note: the packet files `mai_CONTRACTS.md` / `mai_UI_WORKSPACE.md` (sections C07, C10, SERVICES row) were **not present in any worktree** at dispatch (all branches at `8e19ffc` with `.gitignore` + `IDEA.md` only). This decision is anchored to the corresponding `IDEA.md` sections above; no API is invented from a proposed filename. Reconciliation with the frozen contract vocabulary is owed at the phase gate.

All package names, versions, endpoints and defaults below were verified against primary sources on 2026-10-12 (source list at the end). Anything not verifiable today is marked **unverified**.

## 1. Decision summary

| Concern | Decision |
|---|---|
| Operational memory | Hindsight `ghcr.io/vectorize-io/hindsight` as a Compose-internal service (API `:8888`, UI `:9999`, no published host port), version-pinned at integration time |
| Hindsight storage | The stack PostgreSQL instance, dedicated database `hindsight`, pgvector extension (Hindsight default backend) — no embedded `pg0` in deployment |
| Search | SearXNG `docker.io/searxng/searxng` as a Compose-internal service, no published host port, per `IDEA.md` §12 |
| Search auxiliary | Valkey `docker.io/valkey/valkey:9-alpine` (official template pairing) for limiter/bot-detection state |
| Embedding endpoint | The already-agreed configurable OpenAI-compatible endpoint (`IDEA.md` §8), embeddings API `POST {base_url}/v1/embeddings` — **not** chat completions |
| First embedding model | `text-embedding-3-small`, native 1536 dimensions, no `dimensions` reduction in v1 |
| Documentation vector store | PostgreSQL + pgvector in the controller's own database: `vector(1536)` columns, HNSW `vector_cosine_ops` |
| New dependencies introduced by this decision | `hindsight` image, `searxng/searxng` image, `valkey/valkey:9-alpine`, pgvector extension (via `pgvector/pgvector`-based Postgres image or equivalent). Nothing else; no additional vector-database service. |

## 2. Hindsight (operational memory)

### 2.1 Supported packages and distribution channels

| Channel | Artifact | Verified state 2026-10-12 |
|---|---|---|
| PyPI server package | `hindsight-api` | `0.10.3`; requires Python ≥ 3.11; depends on `hindsight-api-slim[all]==0.10.3`; PyPI `license_expression: MIT` |
| PyPI client | `hindsight-client` | `0.10.3`; Python ≥ 3.10; MIT |
| PyPI embedded | `hindsight-all` (in-process server), `hindsight-litellm` (LLM wrapper) | upstream README instructs `pip install`; PyPI versions **unverified** (not the selected packaging) |
| Docker | `ghcr.io/vectorize-io/hindsight` (`:latest`; version-pinnable) | selected packaging; API `8888`, UI `9999` |
| Helm | `oci://ghcr.io/vectorize-io/charts/hindsight` | exists upstream; not used (Compose delivery per `IDEA.md` §2) |
| npm | `@vectorize-io/hindsight-client` | exists upstream; not required for MAI v1 (Python backend) |

Selection: the **Docker image**, run as a Compose service. Bare-metal `pip install hindsight-api` into the controller is rejected: it drags the whole memory engine (Postgres drivers, ONNX extras) into the FastAPI application dependency set and duplicates a process the stack already isolates.

### 2.2 License

Repository `vectorize-io/hindsight`: **MIT** (`LICENSE`, "Copyright (c) 2025 Vectorize AI, Inc."); PyPI metadata agrees (`license_expression: MIT`). Discrepancy to be aware of, not a blocker: the published OpenAPI specification document self-describes as Apache-2.0, and the PyPI package description body contains a stale "Apache 2.0" line. The authoritative artifact license is MIT. MIT obligations: keep the copyright/permission notice in distributions that bundle or redistribute the software.

### 2.3 API surface (from upstream OpenAPI `0.10.3`, fetched 2026-10-12)

- Retain: `POST /v1/default/banks/{bank_id}/memories` — body `{"items": [{"content": "...", ...}], "async": false}`; `items` required; per-item `content` required.
- Recall: `POST /v1/default/banks/{bank_id}/memories/recall` — body `{"query": "...", "types": ["world"|"experience"|"observation"]?, "budget"?, "max_tokens"?}`.
- Reflect: `POST /v1/default/banks/{bank_id}/reflect` (LLM-backed answer generation).
- Health/version: `GET /health` (readiness, fails when DB unreachable), `GET /health/live`, `GET /health/ready`, `GET /version`.
- Built-in MCP endpoint per bank: `/mcp/{bank_id}/`.
- Banks group memories; server scoping (per `IDEA.md` §3/§11) maps to bank-per-server plus explicit shared banks — bank naming is a later contract, not fixed here.

### 2.4 Configuration (selected subset, all verified against the upstream configuration reference, docs version 0.10)

| Variable | Purpose | Decision/default |
|---|---|---|
| `HINDSIGHT_API_DATABASE_URL` | PostgreSQL connection string | points at the stack Postgres, database `hindsight`; embedded `pg0` rejected for deployment |
| `HINDSIGHT_API_DATABASE_SCHEMA` | schema for Hindsight tables | keep `public` of the dedicated `hindsight` database |
| `HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP` | migrations at boot | `true` (default) |
| `HINDSIGHT_API_LLM_PROVIDER` / `_LLM_MODEL` / `_LLM_API_KEY` | extraction/consolidation/reflect LLM | provider `litellm` or an OpenAI-compatible config; **retain is not purely mechanical — an LLM endpoint is a hard auxiliary dependency** |
| `HINDSIGHT_API_VECTOR_EXTENSION` | vector index backend | `pgvector` (default; HNSW) — matches the store decision; `pgvectorscale`/`vchord`/`scann` not adopted |
| `HINDSIGHT_API_TEXT_SEARCH_EXTENSION` | BM25 backend | `native` (PostgreSQL FTS) — no extra extension |
| `HINDSIGHT_API_EMBEDDINGS_PROVIDER` | embedding backend | `openai` with `HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL` set to the same OpenAI-compatible gateway (see §4); default `local` rejected for deployment parity with the doc index |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL` | embedding model | `text-embedding-3-small` (upstream default) |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS` | requested dims for `text-embedding-3` | unset (native 1536) — keep one dimension story |

### 2.5 Persistent stores and required auxiliary services

- Store: PostgreSQL + pgvector (Hindsight's default `HINDSIGHT_API_VECTOR_EXTENSION=pgvector`, HNSW). Iterative-scan support requires pgvector ≥ 0.8.0; current release is v0.8.7. Entity resolution also uses `pg_trgm` thresholds (`pg_trgm` is a standard PostgreSQL contrib extension; presence in the selected Postgres image is confirmed by the 00-verify smoke in §5).
- Alternative store backends (pgvectorscale, vchord, scann, Oracle 23ai) exist upstream and are **not adopted**; switching later is a migration event with its own rebuild semantics (upstream: extension switch with existing data errors out and needs explicit migration).
- Auxiliary services required:
  1. PostgreSQL (already in the stack) with pgvector — via `pgvector/pgvector:pg<MAJOR>` image or a distribution with the extension; the pgvector-based image is what Hindsight's own external-Postgres compose template uses (`pgvector/pgvector:pg18` default).
  2. An LLM endpoint (retain extraction / consolidation / reflect). Without it, retain cannot complete.
  3. An embeddings endpoint (§4).
- Persistence layout: everything in the `hindsight` database; no bind-mounted data directory needed. The embedded-`pg0` volume mode is dev-only.

## 3. SearXNG (bundled metasearch)

### 3.1 Packages and packaging

- Project `searxng/searxng`, **AGPL-3.0** (GitHub license metadata). Docs snapshot verified: `2026.10.11+ad46ebd7d`.
- Container images: `docker.io/searxng/searxng` with date-based tags (`latest`, e.g. `2026.3.25-541c6c3cb`), GHCR mirror `ghcr.io/searxng/searxng` (DockerHub rate-limit escape hatch). Image ≈ 265 MB.
- The historical `searxng/searxng-docker` repository is **superseded**; the current official packaging is the compose template inside the main repo (`container/docker-compose.yml` + `container/.env.example`), services `core` (container `searxng-core`, port 8080) and `valkey` (container `searxng-valkey`, `docker.io/valkey/valkey:9-alpine`), volumes `./core-config/:/etc/searxng` and named volume `/var/cache/searxng`.
- In-process server is Granian (`GRANIAN_*` envs) in current images.

Decision: adopt the official two-service shape (SearXNG + Valkey) inside MAI's compose project, internal network only, **no published host port** (`IDEA.md` §12 proposed default). Version pinning happens at integration time (`SEARXNG_VERSION`).

### 3.2 API and configuration facts that constrain MAI

- Query API: `GET/POST /search` (`GET /` equivalent), `q` required; structured output via `format=json|csv|rss`. **A format not enabled in settings returns 403.** Default `search.formats` is `[html]` only — MAI's settings must add `json`. This is the single most important deployment setting for MAI's search capability.
- Other query parameters: `categories`, `language`, `pageno`, `time_range` (`day|month|year`), `safesearch`. Admin introspection: `GET /config`.
- `valkey.url` (env `SEARXNG_VALKEY_URL`) enables limiter/bot-detection and rate-limiting state; upstream default is disabled (`valkey.url: false`). Point it at the bundled Valkey service; without it SearXNG runs stateless but exposes the instance to upstream-engine rate limiting.
- Page fetching/extraction stays separate from search (`IDEA.md` §12): SearXNG returns result metadata; retrieval of page content is MAI's own capability — no dependency implied here.

### 3.3 Persistent stores and auxiliary services

- SearXNG itself keeps **no database**: state is the settings volume and a cache volume (favicon cache etc.). Nothing to back up beyond config.
- Required auxiliary service: Valkey (`valkey/valkey:9-alpine` in the official template) when limiter/bot-detection is on — adopted.
- Outbound dependency: configured upstream engines; queries leave the host toward upstream search engines. Query sanitization (no secrets/host identifiers in `q`) is a policy requirement from `IDEA.md` §12/§14, enforced in the capability layer, not in SearXNG configuration.

## 4. Embedding decision (first endpoint/model/vector store)

### 4.1 Choice

- **Endpoint:** the administrator-configured OpenAI-compatible connection from `IDEA.md` §8, embeddings API `POST {base_url}/v1/embeddings` with `{"model": ..., "input": [...]}` → `{"data": [{"embedding": [...]}]}`. The embeddings endpoint is a **different API from chat completions**; MAI must never send embedding requests to `/chat/completions` or assume a chat model can serve vectors.
- **Model:** `text-embedding-3-small`. Native output 1536 dimensions; the `dimensions` parameter can shorten it (Matryoshka-trained) — **not used in v1**; native dims only, so schema and provider stay in one state. It is also Hindsight's own default for the `openai` embeddings provider, so both consumers (Hindsight memory and the MAI doc index) run the same model from day one.
- **Vector store:** PostgreSQL + pgvector, same instance as the rest of the stack. `vector(1536)` columns with HNSW indexes (`vector_cosine_ops`; HNSW supports up to 2,000 dims for `vector`, 1536 fits). pgvector current release v0.8.7, PostgreSQL License, upstream helper `vector_dims(vector) → integer` for dimension assertions. A dedicated vector-database service (Qdrant/Milvus/Weaviate/…) is **not** adopted — that would be an undocumented dependency addition contradicting the Postgres-default decision in `IDEA.md` §2.

### 4.2 Dimension contract

1. Every vector-bearing table (doc chunks; any future non-Hindsight index) carries index metadata: `(embedding_provider, embedding_model, embedding_dimensions, distance_metric)`. v1 values: `openai-compatible`, `text-embedding-3-small`, `1536`, `cosine`.
2. Column types are pinned: `vector(1536)`. Write path rejects vectors whose length ≠ the registered dimension (enforced twice: by the column type and by `vector_dims()` check in the ingest path).
3. Query path embeds with the same registered model; a consumer that finds configured model/dims ≠ index metadata **refuses to query and demands rebuild** — never silently compares vectors across models or dims.
4. Hindsight-side: `HINDSIGHT_API_EMBEDDINGS_*` variables pin the same model; Hindsight's ONNX provider exposes a dims-mismatch startup guard; the remote-provider equivalent is the contract above plus the §5 smoke assertion of response length.

### 4.3 Rebuild contract

- Canonical sources are the documents in MAI's data root (`IDEA.md` §10); all vector data is **derived** and must be reconstructible: delete → re-chunk → re-embed → re-index.
- Trigger: any change of embedding provider, model, or dimensions; index corruption; version-drift mismatch found at startup.
- Doc index rebuild: full re-embed from source documents; rebuild runs before the retrieval capability is marked healthy.
- Hindsight rebuild: memory banks cannot be re-derived from MAI's document store (retained memories ≠ documents). Hindsight's own API provides bank export/import (`POST .../transfer/export`, `POST .../transfer/import`) and document-transfer endpoints; the accepted operational rule is: **memory banks are backed up (Postgres dump of the `hindsight` database), never silently rebuilt.** A provider switch inside Hindsight therefore requires re-embedding its store (or a fresh bank) — planned, not improvised.
- No incremental model-migration: one active (provider, model, dims) per index at a time.

### 4.4 Alternatives recorded (rejected for v1, kept visible)

| Alternative | Why not first |
|---|---|
| Hindsight `local` provider (`BAAI/bge-small-en-v1.5`, 384-dim, MIT) or `onnx` (`intfloat/multilingual-e5-small`) | in-process model download in the API container; different model than the doc index unless MAI also embeds docs locally; deferred until an offline-capable profile is designed (`IDEA.md` §18.8) |
| TEI (`HINDSIGHT_API_EMBEDDINGS_PROVIDER=tei`) self-hosted service | an extra stateless service + GPU/CPU sizing before any usage data; revisit when local-model support lands |
| Dedicated vector DB | contradicts "PostgreSQL in the default stack"; adds operational surface pgvector already covers at MAI's scale (pgvector targets <10M vectors on HNSW) |
| `dimensions` reduction (e.g. 512) of `text-embedding-3-small` | saves storage, adds a non-native dims story across two consumers for no v1 need |

## 5. Real-service smoke commands (for the approved disposable stack)

Ownership note: the full round trip in a disposable stack is owned by `00-verify` (feature-gate scenario). These commands were **derived from the primary sources cited above and are not yet executed by this card** — no disposable stack is authorized on this coordinator host, and no dependency installs are permitted here. `00-verify` must run them, record actual outputs, and correct this document before the contracts freeze. All URLs are Compose-internal service names; `$KEYS` come from `.env`, never inlined.

```bash
# S1. SearXNG is up and serves JSON (requires search.formats: [html, json])
curl -fsS 'http://searxng:8080/search?q=searxng&format=json' | jq '{number_of_results, first: .results[0].title}'
# expect: JSON object with a non-null .results[0].title; HTTP 403 here means the settings volume lacks "json" in search.formats

# H1. Hindsight health (readiness includes DB reachability)
curl -fsS 'http://hindsight:8888/health'
curl -fsS 'http://hindsight:8888/version'

# H2. Retain then recall (bank names are disposable)
curl -fsS -X POST 'http://hindsight:8888/v1/default/banks/mai-smoke/memories' \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"content":"MAI smoke test: the disposable stack retains this fact"}],"async":false}'
curl -fsS -X POST 'http://hindsight:8888/v1/default/banks/mai-smoke/memories/recall' \
  -H 'Content-Type: application/json' \
  -d '{"query":"what did the smoke test retain?"}'
# expect: retain returns operation/fact payload; recall returns matching facts
# failure modes to record: LLM endpoint unreachable (retain needs the extraction LLM), DB/pgvector missing

# E1. Embedding round trip on the configured OpenAI-compatible endpoint
curl -fsS "${MAI_EMBEDDINGS_BASE_URL%/}/embeddings" \
  -H "Authorization: Bearer ${MAI_EMBEDDINGS_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"text-embedding-3-small","input":"dimension contract probe"}' \
  | jq '{model, dims: (.data[0].embedding | length)}'
# expect: dims == 1536. NB: {base_url} is the connection root WITHOUT /v1 per OpenAI-compatible convention; verify against the actual gateway's path layout and record it.
# This probes the embeddings API; it must never be pointed at /chat/completions.

# V1. pgvector round trip (dimension pinning + cosine operator + helper)
# service names below are MAI compose choices; adjust to the actual project's service names
docker compose exec postgres psql -U mai -d mai -c "
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- Hindsight entity resolution dependency
  CREATE TABLE smoke_vec (id int primary key, v vector(1536));
  INSERT INTO smoke_vec
    SELECT 1, ('[' || array_to_string(array_fill(0.1, ARRAY[1536]), ',') || ']')::vector;
  SELECT vector_dims(v) FROM smoke_vec;      -- expect 1536
  SELECT v <=> ('[' || array_to_string(array_fill(0.2, ARRAY[1536]), ',') || ']')::vector FROM smoke_vec;  -- expect a finite cosine distance
  DROP TABLE smoke_vec;"
```

## 6. Consequences and open items

- Compose gains exactly three new services: `hindsight`, `searxng`, `valkey`; the Postgres image must be a pgvector-enabled one (`pgvector/pgvector:pg<MAJOR>` or equivalent). Final Postgres major/image is the runtime-environment card's decision; this document constrains it to "pgvector ≥ 0.8.0 + pg_trgm available".
- Versions pinned at integration time; this document records verified-availability facts, not final pins.
- Bank/index naming, chunking parameters, retrieval/reranking (`IDEA.md` §18.7 remainder) are later contracts and deliberately not fixed here.
- `HINDSIGHT_API_LLM_PROVIDER` for MAI's stack (direct OpenAI-compatible vs `litellm`) is chosen together with the model-connection card; the contract requirement is only that retain/recall/reflect work against the configured endpoint.

## 7. Sources (all fetched 2026-10-12; raw evidence in `mai_evidence/00-knowledge-packaging/`)

1. Hindsight repo + `LICENSE` (MIT) — https://github.com/vectorize-io/hindsight · README (`main`).
2. PyPI JSON: `hindsight-api` 0.10.3, `hindsight-client` 0.10.3 (license_expression MIT, requires-python, deps).
3. Hindsight configuration reference v0.10 — https://hindsight.vectorize.io/developer/configuration (DB, vector extension, embeddings provider/vars).
4. Hindsight HTTP API 0.10.3 — https://hindsight.vectorize.io/openapi.json (paths, request schemas, license note).
5. Hindsight external-Postgres compose template — `docker/docker-compose/external-pg/docker-compose.yaml` (`pgvector/pgvector:pg18`).
6. SearXNG container installation docs `2026.10.11+ad46ebd7d` — https://docs.searxng.org/admin/installation-docker.html (images, volumes, valkey, Granian, superseding `searxng-docker`).
7. SearXNG `container/docker-compose.yml` + `.env.example` and default `searx/settings.yml` (`formats: [html]`, `valkey.url`) — https://github.com/searxng/searxng.
8. SearXNG search API doc — `docs/dev/search_api.rst` (formats/403 rule, parameters); AGPL-3.0 via GitHub license metadata.
9. pgvector README (`master`, release v0.8.7) — dims limits (vector ≤2000 indexed, halfvec ≤4000), HNSW operators, `vector_dims()`, PostgreSQL License.
10. `BAAI/bge-small-en-v1.5` model config (hidden_size 384, MIT) — https://huggingface.co/BAAI/bge-small-en-v1.5.
11. OpenAI embeddings guide — `text-embedding-3-small` 1536-dim default, `dimensions` shortening supported (developers.openai.com/api/docs/guides/embeddings).
