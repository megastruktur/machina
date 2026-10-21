# AGENTS.md — Machina (M.A.I.) Repository Guidelines

## 1. Mandatory Pagination Invariant (All Layers)

**HARD RULE: Every function, endpoint, agent tool, store query, and UI loader that loads a collection or variable amount of items MUST be paginated.**

Unbounded collection loads clutter panels, exhaust agent context windows, and risk exceeding the 64 KiB capability dispatcher output budget.

### Backend HTTP Endpoints (`backend/src/mai/api/*.py`)
- Every collection `GET` route MUST accept `limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT)` and `offset: int = Query(default=0, ge=0)`.
- Responses MUST return a page envelope containing the sliced items plus `limit`, `offset`, and `total` (the pre-slice matching count).
- SQL queries MUST apply deterministic ordering (`ORDER BY ... LIMIT :l OFFSET :o`) and compute `COUNT(*)` under the same filter predicate.

### Agent Capabilities & Internal API Tools (`backend/src/mai/capabilities/*.py`)
- Every tool that returns a list of records (e.g., `app.discover`, `app.list_adopted`) MUST declare `limit` (with a bounded default and `ge=1, le=...`) and `offset` (`default=0, ge=0`) on its Pydantic `payload_model` with explicit `Field(description=...)`.
- The executor MUST slice the ordered collection by `[offset : offset + limit]` and return pagination metadata (`limit`, `offset`, `total`, `has_more`) alongside the items so the agent knows when additional pages exist and how to fetch them.

### Frontend Client & Panel Loaders (`frontend/src/lib/**`)
- Every client method in `frontend/src/lib/api/client.ts` and panel service bag that fetches a collection MUST accept `page?: PageQuery` (`{ limit?: number; offset?: number }`).
- Every UI panel/drawer component that loads a collection MUST:
  1. Fetch a bounded initial page (e.g., `PAGE_LIMIT = 10` for Chat History in `ChatPanel.svelte` / `ChatDrawerSection.svelte`; bounded `PAGE_LIMIT` for domain panels).
  2. Track `offset`, `total`, `hasMore`, and `loadingMore` state.
  3. Provide a `loadMore*()` function that requests `{ limit: PAGE_LIMIT, offset }` and appends deduplicated rows.
  4. Render the list inside a bounded scrollable container (`overflow-y: auto` with an explicit `max-height` where appropriate) and render a **"Load more"** control whenever more items remain (`hasMore`).

---

## 2. Internal API → Agent Tool Contract (`@internal_api_tool`)

- All `app.*` and internal API capabilities in `backend/src/mai/capabilities/internal_api.py` are validated fail-closed at import and composition time via `assert_agent_ready_descriptor`:
  - Tool `description` must be at least 30 characters and explain what the tool does.
  - Every field on `payload_model` (including nested `BaseModel` fields and array item models) must declare `Field(description=...)` of at least 12 characters.
- Serialized tool output envelopes must remain strictly under the 64 KiB `CapabilityDispatcher` cap (`_bound_internal_api_envelope`).
- Whenever backend FastAPI route schemas change, regenerate `frontend/src/lib/api/schema.generated.ts` using the command documented at the top of `frontend/src/lib/api/client.ts`.

---

## 3. UI & Workspace Conventions

- **Svelte 5 Runes only:** Use `$state`, `$derived`, `$effect`, and `$props`. Never introduce module-level mutable singletons (SSR safety).
- **Explicit Server Scope (C01 rule 2 / C11):** Panels read their target from `panel.scope` (`server_id`) and receive capabilities through `panel.services.api`. Never use hidden global server state.
- **Wire Honesty (C08 rule 6):** Always surface `loading`, `empty`, `error`, and `ready` states honestly; never fabricate success or hide truncation/pagination counts.

---

## 4. Verification & Test Execution

- **Backend tests:** Run from `backend/` via `uv run pytest`.
- **Frontend type/schema check:** Run from `frontend/` via `pnpm check`.
- **Playwright tests (OOM guard):** In-memory Vite/Svelte builds in Playwright consume ~1.3 GB per worker. Always run Playwright with a single worker and bounded Node heap:
  ```bash
  cd frontend && NODE_OPTIONS="--max-old-space-size=1024" pnpm exec playwright test --workers=1
  ```
