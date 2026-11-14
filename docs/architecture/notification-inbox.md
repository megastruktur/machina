# Notification inbox — canonical route and contract reference

- **Campaign:** `notification-inbox` · task `nbi-contracts` · documentation-only.
- **Status:** canonical reference for the unified notification inbox HTTP surface,
  published at the G-ADR ratification of auth-and-origin.md §13. The frozen ADRs
  and contracts.md (C01, C10 rule 3, C01 rule 2 exception) govern; this document
  is the route/contract reference the implementation tasks consume. On any
  conflict, the frozen sources win: auth-and-origin.md §13 (machine ingest
  principal) and the nbi_RESEARCH.md Revision 2/3 frozen sections.
- **Provenance:** HTTP/filter/error contracts from nbi_PLAN.md «Architecture and
  shared contracts» (FROZEN) and nbi_RESEARCH.md Rev 2 (§R2-F1 search/filter
  field set, §R2-F2 insert arbiters) / Rev 3 (§R3-F2 body budget ordering,
  §R3-F3 identical scope failures, §R4-F2 filter semantics). The ratified
  addendum draft (SHA-256 `3fffc8f07f81b6b28b86f75ebbdbc92a3667f846e30b2d67edb5522f70ee73b7`)
  was approved by the captain at G-ADR (2026-11-13T20:56:00Z, verbatim answer
  «approved») before this reference was published.

## 1. Domain vocabulary

- ONE durable inbox table `notification_alerts` serves every notice kind;
  `origin` discriminates: `finding | job_result | external` (widened by the
  notification-inbox addendum; migration 0017).
- Server-scoped notices carry `server_id`; **global** notices have `server_id
  NULL` (no sentinel UUID). Per-server uniqueness stays on the existing
  `UNIQUE(server_id, dedup_key)`; global rows dedup on the partial unique index
  `uq_notification_alerts_dedup_global ON (dedup_key) WHERE server_id IS NULL`.
- `severity` is a real column (`info | warning | critical`, default `info`),
  replacing the `[severity=x]` summary-tag grammar for both writer and reader.
- `source` (≤64 chars) is the sender identity of external notices (defaults to
  the ingest key's label); NULL for finding/job_result. `body` (≤4096 chars) is
  the full external message text; NULL for finding/job_result (summary remains
  their text).
- Frozen bounds constants in `backend/src/mai/contracts/observations.py`:
  `MAX_NOTIFICATION_BODY_CHARS=4096`, `MAX_NOTIFICATION_SOURCE_CHARS=64`,
  `MAX_INGEST_BODY_BYTES=16384`, `INGEST_RATE_WINDOW=60` (requests/minute,
  durable per-key fixed window, DB-backed, restart-safe).

## 2. Machine ingest principal (summary)

`POST /api/v1/notifications/ingest` is the ONLY route that authenticates a
`Authorization: Bearer <key>` machine ingest principal (auth ADR §13). Keys
grant exactly one capability: appending a notice to the unified inbox. No
session, no grants, no read access, no CSRF/origin surface (cookieless lane).
G1 is preserved: in setup state the ingest route answers `401` like every
non-health route. Keys are minted/revoked by the authenticated admin
(§6); plaintext is returned exactly once; only SHA-256 is stored.

## 3. HTTP surface (canonical — ONE contract)

| Route | Auth | Behavior |
|---|---|---|
| `POST /api/v1/notifications/ingest` | Bearer key ONLY | body budget 16384 bytes BEFORE validation AND BEFORE key validation (Content-Length pre-check + bounded read → 413 `body_too_large` regardless of key validity — no 401-vs-413 key-validity oracle; rate window counts the request either way); payload `{title, body?, severity?, server_id?, source?, dedup_key?, context_ref?}`; source defaults to key label; dedup_key optional → `external:<sha256(source\|dedup_key)[:32]>`, absent → unique per message; scope violations (unknown OR foreign server for scoped key; any server_id for global key) → IDENTICAL 422 `invalid_scope` (byte-for-byte, no existence oracle); rate window → 429; bad/revoked/expired/absent key → 401 (value-free, G1 setup-state included); success `{alert_id, disposition: created\|deduped}`, replay idempotent 200 |
| `GET /api/v1/notifications` | session | canonical filters: `server_id` OR `scope=global` (mutually exclusive, 422 if both); `include_global` (only with server_id; default true); `unread` bool; `origin`; `source`; `q` (ILIKE over title, summary, body, source — ONE field set); `limit` (default 50, ≤200) / `offset`; ORDER BY created_at DESC, alert_id DESC; COUNT same predicate; PageEnvelope + has_more |
| `GET /api/v1/notifications/unread-count` | session | `{total: int}` (same filter surface as list) |
| `POST /api/v1/notifications/{id}/ack` | session→origin→CSRF | mark read; idempotent; evidence preserved |
| `POST /api/v1/notifications/{id}/unack` | session→origin→CSRF | mark unread; idempotent; re-arms finding reminder logic (documented) |
| `POST /api/v1/notifications/mark-all-read` | session→origin→CSRF | one UPDATE under the SAME canonical filters; receipt `{updated: int}` |
| `GET /api/v1/notifications/events` | session + origin (before first byte) | SSE wakeup-only: identity-only frames `{kind: created\|updated, alert_id, server_id}`; NO content, NO cursor; client refetches durable store on wakeup/reconnect (S3) |
| `GET /api/v1/ingest-keys` | session | bounded page of keys (label, scope, created, last_used, expires, revoked) — NEVER key material |
| `POST /api/v1/ingest-keys` | session→origin→CSRF | mint; plaintext returned ONCE |
| `POST /api/v1/ingest-keys/{id}/revoke` | session→origin→CSRF | revoke; next ingest fails closed |
| existing `/api/v1/monitoring/servers/{id}/notifications*` | unchanged | + per-server unack route; severity reader switches to column; shapes additive only |

All routes live under the `/api/v1` prefix and use the C01 structured error
envelope (`code`/`message`/`request_id`). List endpoints are bounded/paginated
(C01 rule 5; AGENTS pagination invariant). Session mutations keep the
session → origin → CSRF order (auth ADR §5); the ingest lane is the documented
exception to both the cookie boundary and C01 rule 2's ambient-scope rule
(contracts.md notification-inbox addendum).

## 4. Filter semantics (frozen, R4-F2)

- No `server_id` and no `scope=global` → ALL notices (every server + global);
  `include_global` is ignored (nothing to exclude).
- `server_id=<uuid>` → that server's notices; `include_global` defaults to
  `true` (global notices included alongside); `include_global=false` → strictly
  that server's notices.
- `scope=global` → only NULL-server (global) notices. `server_id=global` is
  NOT a value; `scope=global` is the explicit alternative to `server_id`,
  mutually exclusive — sending both is `422` (`validation_failed`).
- Ordering and count: `ORDER BY created_at DESC, alert_id DESC`, `COUNT(*)`
  under the same filter predicate, page envelope with `limit`, `offset`,
  `total`, `has_more`.
- Canonical search field set (ONE list, `q`, ILIKE): `title, summary, body,
  source` — nothing else. Canonical filter set (ONE list):
  `server_id | scope=global (mutually exclusive), include_global, unread,
  origin, source, q, limit, offset`.

## 5. Error vocabulary

| Status | `code` | When |
|---|---|---|
| 401 | `unauthenticated` | absent/malformed/unknown/revoked/expired key on ingest; no session anywhere else; G1 setup state (value-free, no existence oracle) |
| 413 | `body_too_large` | request body over `MAX_INGEST_BODY_BYTES=16384`, checked BEFORE payload validation AND BEFORE key validation; no row written; rate window still counts the request |
| 422 | `invalid_scope` | scope violations on ingest — unknown OR foreign server for a scoped key; ANY `server_id` for a global key — byte-for-byte identical response (no existence oracle, R3-F3); also both `server_id` and `scope=global` on session list routes (`validation_failed` shape, C01 envelope) |
| 422 | `validation_failed` | payload bounds violations on ingest (title 200, body ≤4096, source ≤64, severity vocabulary, unknown fields rejected) |
| 429 | `throttled` | per-key fixed window exceeded (60 requests/minute, durable) |
| 503 | `service_unavailable` | database unavailable (C01 envelope) |

Ingest auth failures never reveal key material or key validity distinctions
(bad/revoked/expired/absent are the same value-free `401`).

## 6. Key management (Sources tab contract)

- `POST /api/v1/ingest-keys` payload: `{label (1..64), server_id?}` — omitting
  `server_id` mints a GLOBAL key (may only append server-less notices); with
  `server_id` the key is scope-pinned to that server. Response carries the
  plaintext key EXACTLY ONCE; only SHA-256 is stored (auth ADR §13 key
  lifecycle). Keys may carry optional `expires_at`; revocation is effective on
  the next request.
- `GET /api/v1/ingest-keys` returns metadata only (label, scope, created,
  last_used, expires, revoked) — never key material.
- `POST /api/v1/ingest-keys/{id}/revoke` sets `revoked_at`; the next ingest
  from that key fails closed with the same value-free `401`.
- Secret hygiene (C03 rule 6 discipline): keys are never logged, never in
  error envelopes/evidence, never persisted by the UI; durable operator copies
  follow the 1Password workflow.

## 7. Canonical publish example and idempotent replay

Console example (placeholders `$ORIGIN`, `$MAI_INGEST_KEY` — never real
secrets). The ingest lane is cookieless: no `Origin`, no CSRF header, no
cookie.

```console
$ curl -i -X POST https://$ORIGIN/api/v1/notifications/ingest \
    -H "Authorization: Bearer $MAI_INGEST_KEY" \
    -H "Content-Type: application/json" \
    -d '{"title":"disk almost full","body":"/var usage 94% on db-1","severity":"warning","source":"host-monitor","dedup_key":"disk-/var-db-1","context_ref":"https://monitoring.example/host/db-1"}'
HTTP/2 200                      # {"alert_id":"…","disposition":"created"}
```

Idempotent replay:

- With the same `dedup_key`, a replayed publish does NOT create a second row:
  the insert uses `ON CONFLICT ... DO NOTHING` against the scope's arbiter
  (composite `UNIQUE(server_id, dedup_key)` for server-scoped notices; partial
  index `ON (dedup_key) WHERE server_id IS NULL` for global notices) and
  returns the committed row's identity with `disposition: "deduped"` and HTTP
  `200` — idempotent at the HTTP level, byte-identical success shape.
- Without `dedup_key`, the derived key is unique per message (every publish
  creates a new notice; the derived form is `external:<sha256(source|dedup_key)[:32]>`
  when a `dedup_key` IS supplied).
- A global key may NOT name `server_id` in the payload (→ 422 `invalid_scope`);
  global publishes always land as NULL-server rows.
- The rate window counts every request — created, deduped, 413, and 422 alike.

## 8. Live channel

`GET /api/v1/notifications/events` is a wakeup-only SSE stream (session +
origin checked before the first byte). Frames carry identity only:
`{kind: created|updated, alert_id, server_id}` — no content, no cursor.
Clients refetch the durable list/count on every wakeup and reconnect; delivery
is at-least-once with loss bounded by one coarse tick (5 s) — wakeups are an
optimization, never a correctness dependency. The hub is a SessionChannelHub
sibling: one LISTEN connection per process, channel `mai_notification_alerts`,
bounded outbox, dead-client eviction.

## 9. Runtime testing anchors

- Amendments present: `grep -n "invalid_scope\|scope=global\|16384\|external"
  docs/architecture/contracts.md docs/architecture/notification-inbox.md` — all
  canonical terms present.
- Verbatim application: §13 in auth-and-origin.md diffed against
  `plans/notification-inbox/nbi_ADR_ADDENDUM_DRAFT.md` (normalized whitespace)
  shows no substance difference (the change-control note additionally carries
  the G-ADR ratification record, as mandated).
- Repo still imports: `cd backend && uv run python -c "from mai.api.app
  import create_app; create_app()"` — docs-only change cannot break the app
  factory.
