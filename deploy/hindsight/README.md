# Hindsight — bundled operational memory (Compose service)

Cards: `11-hindsight-compose` (this packaging) · contract `docs/architecture/contracts.md`
C07 rule 7 · decision `docs/architecture/knowledge-packaging.md` §2/§4 (ADR
`5d6e0a1`) · scenario registry KB-H1 (health/version) in
`docs/architecture/runtime-scenarios.md`.

## What is deployed

| Concern | Value |
|---|---|
| Service | `hindsight` in `compose.yaml`, opt-in via `--profile memory` |
| Image | `ghcr.io/vectorize-io/hindsight:1.0.0` |
| Digest at integration | `sha256:a88678c038b0e4340822b02a5efc566c56d8176b5bffa731bc433fd37f978e4c` (image built 2026-10-13, revision `39c65ffe`) |
| License | **MIT** — upstream `LICENSE`, "Copyright (c) 2025 Vectorize AI, Inc." (matches PyPI `license_expression: MIT`. Recorded discrepancy, not a blocker: the published OpenAPI spec document self-describes as Apache-2.0; ADR §2.2 names the repo/PyPI MIT as the authoritative artifact license. MIT notice obligations apply only if the image is redistributed.) |
| Listener | API `:8888`, UI (control plane) `:9999` — **internal to the `mai` network, no published host port** |
| In-process Python packages | none by decision (`hindsight-api/all/client/litellm` are held out of the controller by `backend/tests/test_optional_dependencies.py`, card `11-dependencies`) |

The memory capability is a stack-owner opt-in like the search pair: the default
start set (`docker compose up -d`) contains no memory service and makes no
network listener for it. Enable it explicitly:

```bash
docker compose --profile memory up -d
```

## Required `.env` configuration (stack owner)

The service renders `docker compose config`-clean without these variables; the
LLM values interpolate empty and **upstream fails closed at container start**
instead (measured on 1.0.0: without a key for the selected provider the
container exits 1 with `LLM API key is required. Set
HINDSIGHT_API_LLM_API_KEY environment variable.`).

| Variable | Meaning |
|---|---|
| `HINDSIGHT_API_LLM_PROVIDER` | Defaults to `openai` in the service env (upstream default). Any provider name upstream supports; `litellm` needs no API key. |
| `HINDSIGHT_API_LLM_MODEL` | Extraction/consolidation/reflect model. Empty ⇒ provider default (`gpt-4o-mini`). |
| `HINDSIGHT_API_LLM_BASE_URL` | Point at the same OpenAI-compatible gateway the backend uses (ADR §2.4). Empty ⇒ provider built-in default (for `openai`: api.openai.com). |
| `HINDSIGHT_API_LLM_API_KEY` | **Required for keyed providers.** Missing ⇒ the container refuses to serve: it exits 1 naming the missing variable and — with `restart: unless-stopped` — stays in a visible restart loop with the cause in `docker compose logs hindsight` until the config is fixed. It never serves degraded. |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL` | Embeddings endpoint root; same gateway as the doc index (ADR §4: `POST {base}/v1/embeddings`, never `/chat/completions`). |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY` | Embeddings credential from the Compose `.env` only. |

Not configurable here (frozen by ADR §4): embeddings provider is pinned to
`openai` with `text-embedding-3-small` and native 1536 dimensions — the
upstream defaults (`local`/`onnx`, model download into the container) are
rejected for deployment parity with the document index; one dimension story
across both consumers (C07 rule 3).

## Persistence, restart and backup ownership (explicit)

- **All Hindsight state lives in the dedicated `hindsight` database of the
  stack Postgres.** The service container holds no volume, no bind mount and
  no embedded store (ADR §2.5 rejects embedded `pg0` for deployment).
- `hindsight-db-init` (one-shot, memory profile, idempotent) creates the
  `hindsight` database if absent — including on stacks where the Postgres
  volume predates enabling the memory profile. Hindsight's own startup
  migrations (`HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP` default true) create
  the `vector`, `pg_trgm` and `btree_gin` extensions plus its tables inside
  that database (measured on 1.0.0); the `mai` database is never touched by
  them.
- **Restart**: `restart: unless-stopped` (same posture as `mai-worker`). A
  crash is restarted by Docker; a manual stop is honored. Data survives any
  restart by construction — it is never in the container.
- **Backup**: `pg_dump` of the `hindsight` database, independent of the `mai`
  database dump (C07 rule 3: memory banks are backed up, never silently
  rebuilt; a provider/model switch needs planned re-embedding or a fresh
  bank). Restore = recreate the empty `hindsight` database, re-apply the dump,
  start the service.
- **Deletion**: `docker compose --profile memory down` keeps the database
  (it belongs to the shared Postgres volume `postgres-data`); dropping the
  `hindsight` database is the explicit data-deletion step and also removes
  every memory bank.

## What health means — and what it does not

The compose healthcheck probes `GET /health` inside the container. Upstream
`/health` is readiness: it fails when the database is unreachable, so a green
probe means migrations ran and the dedicated namespace is reachable (KB-H1).
It is **not** proof that retain/recall work — that is the LLM-backed round
trip of KB-H2, owned by `11-memory-adapter`/`11-verify` on the assembled tip.
Retain/recall failure visibility, bank-per-server scoping and shared-bank
naming are adapter contracts (C07 rule 6), not compose settings.
