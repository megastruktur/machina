# ADR 0001 — Single-admin web authentication and browser-origin architecture

- **Status:** FROZEN P00 decision (`mai-00-web-auth`). Authoritative for P00 consumers; becomes binding on implementation phases upon P00 phase-gate acceptance (`docs/architecture/contracts.md` incorporation by `00-contracts`). Changes after freeze require coordinator reconciliation (`mai_EXECUTION.md` §3).
- **Date:** 2026-10-12
- **Card:** `mai-00-web-auth` · documentation-only · exclusive resource `architecture-decision:00-web-auth`
- **Base:** `8e19ffc` (`megastruktur/mai-00-web-auth`)
- **Sources:** `IDEA.md` (user-approved committed spec) §1 (initial user: the project's owner), §2, §3, §4, §5, §14, §15, §17 (non-goals), §18 decision 14 (web authentication/exposure open decision); `mai_CONTRACTS.md` C01, C08; `mai_UI_WORKSPACE.md` §1; `mai_SERVICES.md` rows `mai-web`, `mai-api`, `postgres` and §"End-to-end execution route"; `mai_QUESTIONS.md` Q3 (proposal — settled here within P00 decision authority; captain START event 2026-10-12 records Q1/Q2/Q4 answered directly, Q3 delegated to P00).
- **Consumers:** `02-auth-store` (credential + session implementation), `02-auth-api` (HTTP boundary), `02-web-session` (login UI), `01-web` (login uses shared primitives), `00-contracts` (contracts.md), platform/Compose cards (exposure).
- **Provenance discipline:** *user-settled* claims trace only to the committed `IDEA.md` spec or the approved dispatch scope (brief acceptance criteria; autonomous-P00 authority in the campaign approval record). *P00-frozen* claims are selections made by this ADR within that authority — including every Q3 proposal item (password login, `.env` bootstrap, opaque session + server-side revocation, CSRF/origin checks, no signup/RBAC, enrollment-before-root); `IDEA.md` §18 decision 14 lists web authentication/session security and exposure as an open design decision, settled here, not answered by the user directly. *Install-time parameters* (which reverse proxy terminates TLS, LAN-only vs LAN+Tailscale initially, certificate source) are recorded as deployment parameters, not settled here. No mechanism is labeled user-approved beyond these classes.

---

## 1. Decision summary

| Area | Frozen decision |
|---|---|
| Identity model | Exactly **one administrator principal**; password login; no signup, no username registry, no RBAC |
| Credential bootstrap | From `.env` (`MAI_ADMIN_PASSWORD` or `MAI_ADMIN_PASSWORD_FILE`), Argon2id-hashed into Postgres; interactive first-run setup endpoint only while unbootstrapped |
| Session | Opaque 256-bit random token, `HttpOnly; Secure; SameSite=Lax` `mai_session` cookie; server-side Postgres record (SHA-256 of token stored); revocable; survives process restart |
| CSRF/origin | Mutations require session + strict `Origin` allowlist match + double-submit CSRF token; SSE subscription authenticated and origin-checked |
| TLS/proxy | Single TLS-terminating reverse proxy (operator-provided preferred, bundled Caddy alternative); `mai-api`/`mai-web` never published directly; forwarded headers honored only from `MAI_TRUSTED_PROXIES` |
| Exposure | LAN + Tailscale only, via explicit `MAI_PUBLIC_ORIGINS`; loopback HTTP is development-only; no public-internet exposure in v1 |
| Privilege gating | **Enrollment-before-root**: G1 admin-bootstrap gate on the API, G2 target-enrollment gate on every privileged operation; no authentication-disable flag exists |
| API surface | `/api/v1` prefix; principal from session only (never a header/flag); structured errors `code`/`message`/`request_id` (C01) |

Route and file names below that do not yet exist are **frozen design targets** (C01: endpoints in briefs are design targets to implement). Their implementation ownership is listed in §10.

## 2. Identity and administrator model

1. There is one administrator. The principal is `admin`; it is not user-supplied input. Login authenticates **the** administrator, not a selected account.
2. The API obtains the principal exclusively from the validated server-side session (C01). A user-supplied admin flag, role header, or query parameter is never an authorization input. Any privileged request without a valid session is `401` with a structured error; a forged privileged header (e.g. `X-Admin: true`) changes nothing.
3. No signup path exists. Administrator identity is created only by the bootstrap flow (§3).
4. There is no enterprise RBAC, no organization, no multi-tenant concept in v1 — excluded by the committed spec's non-goals (`IDEA.md` §17, "An enterprise multi-tenant/RBAC product or a complete security platform"). IDEA.md §5 grants/policy govern what the single admin's *agent* may do — that is capability policy (C03), not web authentication, and is out of this ADR's scope.

## 3. Credential bootstrap (source `.env` is user-settled: IDEA.md §2/§14; bootstrap mechanism below is P00-frozen)

**Bootstrap sources, in order:**

1. `MAI_ADMIN_PASSWORD_FILE` — path to a file containing the password (recommended; avoids process-env exposure).
2. `MAI_ADMIN_PASSWORD` — direct environment value.

Both are normally supplied through the Compose `.env` file, which is the user-approved primary credential source (IDEA.md §2 table row "Initial credentials source: `.env` files"; §14). Values never enter Git, reports, logs, model context, or documentation; `.env` is already gitignored in this repository.

**Bootstrap rule:**

- At startup, if no administrator credential record exists and a bootstrap source is configured, the controller hashes the password (Argon2id, §3.1) and stores only the hash. The plaintext is not logged, not echoed, and not retained in process memory longer than the hashing call.
- If no credential record exists and no bootstrap source is configured, the API starts in **setup state** (§8 G1): only health and `POST /api/v1/auth/bootstrap` are functional; every other route returns `401`/`403` fail-closed. The setup endpoint accepts `{ "password": "..." }` exactly once — after the first credential record exists it returns `409` permanently.
- If a credential record already exists, bootstrap environment variables are **ignored** for authentication (they are bootstrap-only, not runtime overrides). This prevents a stale `.env` value from silently "fixing" a rotated-away password.

**Rotation/reset:** password change through the authenticated UI is a later feature (owned by `02-auth-store`/settings work; not invented here). Host-side reset = operator with host access stops the controller, removes the credential record via the documented reset procedure (owned by release-docs/ops documentation, P12), then re-bootstraps. Until that procedure exists, this ADR records reset as a **documented manual operation**, not an API capability.

### 3.1 Hash parameters

Argon2id. Minimum accepted parameters (implementation may exceed, not fall below): 64 MiB memory, 3 iterations, parallelism 1, 16-byte random salt. Verification is constant-time; login response is identical for wrong-password and throttled cases (§5.3).

## 4. Session design (C08: signed/opaque session, server-side revocation)

**Token:** 256 bits from a CSPRNG, opaque — the cookie carries no identity, claims, or signature fabricatable client-side. The server stores only SHA-256(token); a database leak yields no usable cookies.

**Cookie:** `mai_session`, Host-only, `Path=/`, `HttpOnly`, `SameSite=Lax`, `Secure` in any non-loopback origin. Not `Strict`: the panel is desktop-first and same-browser deep links/reloads (UI_WORKSPACE §"Selected window controls") must keep the session; `Lax` still blocks cross-site POST replay.

**Server-side record (Postgres; proposed table `admin_sessions`, schema owned by `02-auth-store`, migration `0002_sessions`):**

| Field | Meaning |
|---|---|
| `token_hash` (PK) | SHA-256 of the opaque token |
| `created_at`, `last_seen_at`, `expires_at` | UTC (C01); absolute expiry = created + 30 days |
| `idle_expires_at` | last_seen + 24 h, refreshed on authenticated use |
| `revoked_at` | set on revocation; row retained for audit, never reactivated |

- **Timeouts (frozen):** idle 24 h (sliding), absolute 30 days, both server-enforced. Every authenticated request re-validates the record in the same transaction boundary as authorization; expired/revoked ⇒ `401` and cookie clearance.
- **Concurrency:** up to **16** live sessions (admin desktop + laptop + phone on tailnet). Exceeding the cap evicts the oldest by `last_seen_at`.
- **Revocation:** logout deletes/revokes exactly the presenting session; "revoke all sessions" revokes every record (admin action). Revocation is effective on the next request — no grace window (scenario S2).
- **Fixation:** a fresh token is minted at every successful login; pre-auth tokens are never upgraded to authenticated ones.
- **Restart semantics:** revocation and expiry live in Postgres; process/container restart cannot resurrect a revoked session (`02-auth-store` gate scenario).

## 5. CSRF and origin enforcement (C08)

**Layered rule for every state-changing request** (`POST`/`PUT`/`PATCH`/`DELETE` under `/api/v1`, including chat/tool actions and any stream-auth POST): all three must pass, else `403` with structured error:

1. **Session** valid (§4).
2. **Origin allowlist:** `Origin` header present and exactly matching (scheme+host+port) one entry of configured `MAI_PUBLIC_ORIGINS` (§7). Missing or non-matching `Origin` ⇒ `403`. `Referer` is **not** accepted as a substitute. Non-browser clients (curl, tests) must send the configured origin explicitly; documented examples do so.
3. **CSRF double-submit:** `X-CSRF-Token` header equals the `mai_csrf` cookie value. `mai_csrf` is minted at login and via `GET /api/v1/auth/csrf`, 256-bit CSPRNG, readable by first-party JS, `SameSite=Lax`, Host-only, `Secure` off loopback. It is not `HttpOnly` by design (double-submit); it grants nothing by itself without the session.

`SameSite=Lax` is the third layer (blocks most cross-site cookie attachment), not a replacement for layers 2–3 (covers same-site subdomain confusion and legacy browsers).

**Safe methods:** `GET`/`HEAD`/`OPTIONS` perform no state change; SSE subscription uses `GET` and is authenticated (§6.3). Replay cursor (`Last-Event-ID`/cursor parameter, C08) only ever re-reads persisted events — replay is not a mutation.

**Error behavior:** auth failures return structured errors only — stable `code` (`unauthenticated`, `csrf_rejected`, `origin_rejected`, `throttled`), safe `message`, `request_id`; no stack traces, no secret material, no user-existence signals (C01; `02-auth-api` acceptance "no secret response content").

### 5.3 Login throttling (frozen)

Failed login for the single account: fixed per-source-IP delay starting at 1 s, doubling per consecutive failure, capped at 60 s, plus a global 5-attempts/minute ceiling; all verified server-side in Postgres. `429`/`throttled` responses are indistinguishable (same latency profile) from wrong-password responses. Successful login resets the per-IP counter. Failed attempts land in the audit log with `request_id`, timestamp, source IP — never the attempted password.

## 6. TLS, reverse proxy, and trusted headers (C08: same-origin proxy preferred)

1. **One published listener:** a TLS-terminating reverse proxy is the only component with host-published ports. `mai-api` and `mai-web` listen on the Compose-internal network only; nothing privileged is reachable without passing the proxy and the API's auth boundary (C08: "do not expose privileged backend publicly before auth/perimeter acceptance").
2. **Proxy choice (deployment parameter, not code):** operator's existing reverse proxy is preferred (Q3); a bundled edge (Caddy) in Compose is the approved alternative when no proxy exists. Which one runs on a given host is recorded at install time; the API behavior is identical behind either.
3. **Trusted forwarded metadata:** client IP and scheme are derived from `X-Forwarded-For`/`X-Forwarded-Proto` **only** when the TCP peer is in `MAI_TRUSTED_PROXIES` (CIDR/name list, default: the Compose proxy service). From any other peer, forwarded headers are ignored, and a request whose `X-Forwarded-Host` disagrees with `Host` is rejected `400 request_rejected` (origin-spoof marker). Origin validation (§5) always runs against the reconstructed effective origin.
4. **Proxy obligations:** set `Host`, `X-Forwarded-Proto`, `X-Forwarded-For`; HTTP→HTTPS redirect; HSTS `max-age=31536000`; no path-based auth bypass; pass request bodies unparsed. Certificate provisioning (ACME, LAN-internal CA, or `tailscale cert`) is an install-time parameter.

## 7. Intended exposure: LAN and Tailscale (brief-mandated record; initial target set is an install-time parameter)

**Intended access:** the administrator's browser reaches the panel from (a) the home/lab LAN and (b) the administrator's Tailscale network. Both are first-class; neither is privileged over the other — both terminate at the same authenticated API boundary.

| Deployment parameter | Meaning |
|---|---|
| `MAI_PUBLIC_ORIGINS` | Explicit exact origins allowed by §5.2, e.g. `https://mai.lan.example:8443,https://mai.tailnet-name.ts.net`. **No wildcard entries, no hard-coded addresses** (Q3). |
| `MAI_TRUSTED_PROXIES` | Peers allowed to set forwarded metadata (§6.3). |
| Proxy bind | Chosen at install: LAN interface + tailnet interface, or Tailscale-only via `tailscale serve`. Recorded in install docs, not in code. |

**Frozen exposure rules:**

- Plain-HTTP serving is allowed **only** on loopback (development). `Development loopback is not the final access acceptance test` (Q3) — P02+ verification uses a real TLS proxy origin.
- No public-internet exposure in v1; port-forwarding the controller is out of scope and requires a separate future decision (this ADR does not approve it).
- Tailscale access uses the tailnet's own transport security (WireGuard) plus the TLS proxy above; `tailscale serve`/`tailscale cert` are approved termination options.
- Losing the browser never cancels backend work (SERVICES `mai-web` exposure contract); sessions exist for human convenience, not for job ownership.

## 8. Enrollment-before-root rule (fail-closed privilege)

**G1 — admin bootstrap gate (API-level).** Until an administrator credential record exists, the API serves only:

- `GET /api/v1/health`, and
- `POST /api/v1/auth/bootstrap` (§3, one-shot).

Everything else — every privileged query, mutation, and SSE stream — is refused `401`/`403` in setup state. There is **no** configuration flag, environment variable, or startup mode that disables authentication or opens the API while unbootstrapped or at all. "Open privileged API as an interim production design" is prohibited: a deployment that cannot authenticate cannot serve privileged routes, full stop. Removing this gate is a code change requiring a new architecture decision, never an operational toggle.

**G2 — target enrollment gate (operation-level).** Every privileged operation binds an explicit `server_id` (C01). Before any privileged effect — including any SSH command, and *a fortiori* root/raw-shell class capabilities — the dispatcher verifies:

1. the target profile exists under the data root and is **enrolled/adopted** (C02, C09 — discovery alone never enables execution);
2. the target's SSH host key is pinned **and matches** (C04 — changed key ⇒ reject, no silent re-trust);
3. the capability is granted to this execution context, with root/raw-shell class additionally bound by the approval/policy checks of C03;
4. mode/grant recheck happens immediately before the privileged effect (C03).

The controller's own host is enrolled through the **same** profile mechanism as any remote target (IDEA.md §3: "The installation host is one managed server"); there is no implicit `localhost` bypass of G2, and no alternate shell backdoor in a web route, MCP bridge, cron path, or child agent (SERVICES: end-to-end execution route).

**Consequence:** an unauthenticated request cannot reach privileged effects (G1 + session), and an authenticated request cannot reach an unenrolled/unpinned target (G2). G1/G2 are independently testable (§9).

## 9. Authentication scenarios — concrete request examples (tabletop; executed at phase gate)

> **Status:** these are frozen design examples for later **runtime** execution. Owner of assembled execution: `00-verify` at the P00/P02 gates; runtime producers are `02-auth-store`/`02-auth-api`. This ADR is design evidence, not runtime security validation, and does not claim the product exists. Fixtures: fresh Postgres, controller with bootstrapped credential, proxy container per §6, `MAI_PUBLIC_ORIGINS` containing the proxy origin. Placeholders (`$ORIGIN`, `$TOKEN`) — never real secrets.

**S1 — login (positive) and bootstrap gating**

```console
# G1 in setup state (no credential): privileged route refused
$ curl -i https://$ORIGIN/api/v1/servers
HTTP/2 401                      # code: unauthenticated

# Bootstrap once (or via MAI_ADMIN_PASSWORD at first start; then this is 409)
$ curl -i -X POST https://$ORIGIN/api/v1/auth/bootstrap \
    -H "Origin: $ORIGIN" -H "Content-Type: application/json" \
    -d '{"password":"<initial-credential>"}'
HTTP/2 201                      # credential record created; endpoint now closed

# Login
$ curl -i -c jar.txt -X POST https://$ORIGIN/api/v1/auth/login \
    -H "Origin: $ORIGIN" -H "Content-Type: application/json" \
    -d '{"password":"<credential>"}'
HTTP/2 204                      # Set-Cookie: mai_session=…; HttpOnly; Secure; SameSite=Lax
                                # Set-Cookie: mai_csrf=…; SameSite=Lax
```

Expected: wrong password and throttled login are latency-indistinguishable structured errors; no plaintext material in any response/log.

**S2 — session theft/revocation**

```console
# Attacker replay of a stolen cookie AFTER the admin logs out / revokes:
$ curl -i https://$ORIGIN/api/v1/servers -H "Cookie: mai_session=$STOLEN"
HTTP/2 401                      # revoked server-side; restart does not resurrect (Postgres record)
```

Expected: revocation is effective on the next request with no grace window; `Secure`+`HttpOnly` limit (not eliminate) theft avenues; audit log shows revocation event.

**S3 — CSRF (cross-site mutation denied)**

```console
# Forged request from attacker page (wrong origin, no token):
$ curl -i -X POST https://$ORIGIN/api/v1/servers/$SERVER_ID/commands \
    -H "Origin: https://attacker.example" -H "Cookie: mai_session=$STOLEN" \
    -H "Content-Type: application/json" -d '{"argv":["id"]}'
HTTP/2 403                      # code: origin_rejected
```

Expected: also denied when `Origin` is absent, when `X-CSRF-Token` mismatches `mai_csrf`, and for every other mutating route including chat/tool actions and stream-auth POSTs. `SameSite=Lax` is backup, not the control.

**S4 — reverse-proxy origin (forwarded-header spoofing denied)**

```console
# Direct hit on an unpublished port is impossible (compose-internal only); via the proxy,
# a client may not forge forwarded metadata:
$ curl -i https://$ORIGIN/api/v1/servers \
    -H "X-Forwarded-Host: attacker.example" -H "Cookie: mai_session=$VALID"
HTTP/2 400                      # code: request_rejected (X-Forwarded-Host ≠ Host, trusted peer)
```

Expected: scheme/client-IP derive only from trusted-proxy metadata (§6.3); origin checks run against the reconstructed effective origin; direct backend ports closed on the host.

**S5 — unauthenticated SSE**

```console
$ curl -i -N https://$ORIGIN/api/v1/runs/$RUN_ID/events \
    -H "Accept: text/event-stream"          # no cookie
HTTP/2 401                      # no stream bytes before authz; replay cursor only after auth
```

Expected: subscription requires a valid session and passes the same origin rules; unauthorized clients receive zero event bytes; an authenticated subscriber's reconnect deduplicates via the replay cursor (C08; exact transport finalized by the runtime/API contract task).

**Negative-control matrix** (all must hold; each maps to at least one S above):

| Attempt | Expected |
|---|---|
| No cookie → any privileged route | `401` |
| Forged privileged header (`X-Admin: true`) | `401` (header is not an input, C01) |
| Expired / revoked cookie | `401`, cookie cleared |
| Cross-origin POST (evil origin / missing origin / missing token) | `403` |
| Setup state → any route except health/bootstrap | `401`/`403` (G1) |
| Authenticated request → unenrolled `server_id` | rejected before any SSH effect (G2) |
| Changed SSH host key on enrolled target | reject, no silent re-trust (C04) |
| `X-Forwarded-*` from non-trusted peer | ignored / `400 request_rejected` |

## 10. Ownership and consumer rules (binding after gate acceptance)

| Consumer | Owed by this ADR | Owns subsequently |
|---|---|---|
| `02-auth-store` | Credential hash + session semantics (§3, §4) | `backend/src/mai/auth/sessions.py`, migration `0002_sessions`, exact columns |
| `02-auth-api` | Boundary rules (§2, §5, §8 G1), throttling (§5.3) | `backend/src/mai/api/auth.py`, `app.py`; exact route modules |
| `02-web-session` | Login contract; no credential persistence in `localStorage` | `frontend/src/lib/auth/session.ts`, `routes/login/+page.svelte` |
| `01-web` | Login uses shared primitives (UI_WORKSPACE §1) | primitive composition |
| Platform/Compose cards | Exposure rules (§6, §7): internal-only api/web, single proxy | proxy service definition, port policy |
| `00-contracts` | Incorporates this ADR into `docs/architecture/contracts.md`; registers S1–S5 in `docs/architecture/runtime-scenarios.md` with producers/fixtures | registry mapping |

Route names (`/api/v1/auth/*`), cookie names, and parameter names above are **frozen design targets**: the named owners implement them; renaming requires coordinator reconciliation of this ADR and its consumers. No consumer may add an authentication bypass, an additional admin identity, a header-based principal, or a public listener for `mai-api`/`mai-web`.

## 11. Explicit non-goals and open items (out of this ADR)

- Exact SSE transport choice and replay-cursor wire format — settled by the runtime/API contract task (C08); this ADR fixes only that subscription is authenticated and origin-checked.
- Capability grants, approval binding, and mode semantics — C03/`policy` cards; G2 references them, does not redefine them.
- Password-change UX, session-list UI, audit-log viewer — later product work; semantics above already support them.
- 1Password/other providers as the admin-credential *bootstrap* source — optional plugin territory (IDEA.md §14); not required for v1 bootstrap.
- Fleet/remote enrollment, Windows targets — V2+ (IDEA.md release boundary).
- Public-internet exposure — not approved; separate future decision.

## 12. References

- `IDEA.md` §2 (`.env` credentials row), §3 (installation host is a managed server), §4 (host authorization, installer must make authority explicit), §5 (trusted administration, policy boundary), §14 (credentials and sensitive data), §15 (dashboard/chat) — committed user-approved specification at base `8e19ffc`.
- `mai_CONTRACTS.md` C01 (identities, envelope, principal from session), C08 (single-admin session, CSRF/origin, same-origin proxy, SSE auth) — P00 draft inputs.
- `mai_SERVICES.md` rows `mai-web`, `mai-api`, `postgres`; §"End-to-end execution route" (no alternate shell backdoor).
- `mai_UI_WORKSPACE.md` §1 (login uses shared primitives; login outside the privileged desktop).
- `mai_QUESTIONS.md` Q3 (proposed approach frozen here within P00 authority).
- Evidence: `plans/mai-v1/mai_evidence/00-web-auth/` — the campaign evidence root (gitignored; outside the disposable worktree per source C10; sibling `00-ui-contract/` and `monitoring/` records confirm the convention). Contains the writer's provenance/author-check receipts; coordinator artifact check reads it there.

### 13. Addendum — machine ingest principal (notification-inbox campaign)

Change control: this addendum amends the frozen ADR (`3e79449`) under §10 and
contracts.md «Change control», following the D09/runtime-v2 addendum precedent.
It was ratified by the coordinator (captain) before implementation; the
ratification is recorded in the campaign STATE. G-ADR ratification record:
presented to captain 2026-11-13T20:54:12Z; ratified 2026-11-13T20:56:00Z;
captain verbatim answer: approved; decision: approved; ratified draft SHA-256
`3fffc8f07f81b6b28b86f75ebbdbc92a3667f846e30b2d67edb5522f70ee73b7`.

1. **New principal class, not a new admin identity.** A *machine ingest
   principal* is a scoped API key that grants exactly ONE capability: appending
   a notification to the unified inbox (`POST /api/v1/notifications/ingest`).
   It is NOT an administrator identity, creates NO session, carries NO grants,
   and has NO privileged effect (G2 untouched). §2's «no additional admin
   identity» is preserved; §2's «no header-based principal» is amended ONLY for
   this one route: the ingest route authenticates `Authorization: Bearer <key>`
   (RFC 6750 §2.1 header form). Every other route continues to reject bearer
   credentials; the cookie-session boundary is unchanged.
2. **Key lifecycle.** Keys are minted by the authenticated admin
   (session → origin → CSRF), 256-bit CSPRNG, only SHA-256 stored (the 0002
   `token_hash` pattern). Plaintext is returned exactly once at creation.
   Each key carries: label (1..64), scope (`server_id` UUID or NULL = global
   only), optional `expires_at`, `revoked_at`, `last_used_at`, and a durable
   per-key fixed rate window (60 requests/minute, DB-backed, restart-safe).
   Revocation/expiry take effect on the next request. A key scoped to a server
   may only append notices for that server; a global key may only append
   server-less notices. Payload scope violations, unknown servers, and foreign
   servers all answer the IDENTICAL 422 `invalid_scope` (no existence oracle).
3. **Transport and boundary.** The ingest lane is cookieless: CSRF/origin
   layers do not apply (no cookie to ride). G1 is preserved: in setup state the
   ingest route answers 401 like every non-health route. Request bodies are
   bounded BEFORE validation AND BEFORE key validation:
   `MAX_INGEST_BODY_BYTES = 16384`; Content-Length pre-check plus bounded stream
   read; over-budget → 413 `body_too_large` regardless of key validity (no
   401-vs-413 key-validity oracle), no row written, rate window still counts
   the request. Errors use the C01 envelope; auth failures are value-free (no
   key material, no existence oracle).
4. **Secret hygiene.** Key values are credential material (C03 rule 6
   discipline): never logged, never in error envelopes/evidence, never
   persisted by the UI; durable operator copies follow the 1Password workflow.
5. **Non-goals of this addendum.** No OAuth flows, no token introspection
   endpoint, no machine READ access to any surface, no additional admin
   accounts, no change to session TTL/CSRF/origin rules.
