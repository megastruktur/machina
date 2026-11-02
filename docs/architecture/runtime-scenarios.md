# MAI runtime scenario registry — frozen P00

- **Card:** `mai-00-contracts` (P00 · documentation-only)
- **Status:** FROZEN P00 output; binding on dispatch alongside
  `docs/architecture/contracts.md`. Per `mai_TEST_POLICY.md`, this registry maps each
  gate scenario to a producing task/test entrypoint, prerequisites and expected
  evidence. Missing ownership blocks dispatch; a verifier never invents unowned test
  code and never marks a missing scenario skipped-to-PASS.
- **Provenance:** scenario definitions are frozen in the six P00 ADRs (SHAs in
  `contracts.md` §"Frozen inputs"); this registry adds ownership and gate mapping
  only. It does not re-derive scenario content.

## Status legend

| Status | Meaning |
|---|---|
| `measured` | Executed by the named producer on the dated fixture; evidence path recorded. Reuse only with unchanged provenance (`mai_TEST_POLICY.md` §"Reuse"). |
| `design-frozen` | Definition + fixtures frozen; runtime execution owed at the named gate by the named producer. Commands are design targets until their producer executes them. |
| `tabletop-done` | Desk-checked against the frozen contracts at P00; runtime negative control owned at the named gate. |

---

## AUTH — authentication, session, CSRF/origin (auth ADR `3e79449` §9)

| ID | Scenario (definition) | Producer (owns test/entrypoint) | Verification gate | Fixtures / prerequisites | Expected evidence | Status |
|---|---|---|---|---|---|---|
| AUTH-S1 | G1 setup-state gating, one-shot bootstrap, login sets `mai_session`+`mai_csrf`; wrong-password ≡ throttled latency | `02-auth-api` (boundary), `02-auth-store` (record semantics) | P02 feature gate; assembled execution owner `00-verify` | fresh Postgres, controller with bootstrapped credential, proxy per ADR §6, `MAI_PUBLIC_ORIGINS` incl. proxy origin | HTTP transcripts (S1 console block), no secret material in evidence | design-frozen |
| AUTH-S2 | stolen-cookie replay after logout/revocation ⇒ `401`, restart does not resurrect | `02-auth-store` | P02 gate | session revoked in Postgres; controller restarted | transcript + `admin_sessions` row state | design-frozen |
| AUTH-S3 | cross-site mutation denied (evil/missing origin, missing/mismatched CSRF token) on every mutating route incl. chat/tool/stream-auth | `02-auth-api` | P02 gate | valid stolen session cookie, attacker origin | `403 origin_rejected`/`csrf_rejected` transcripts | design-frozen |
| AUTH-S4 | forwarded-header spoofing denied via proxy (`X-Forwarded-Host` ≠ Host ⇒ `400 request_rejected`); direct backend ports unreachable | `02-auth-api` + platform/Compose cards | P02 gate | proxy container per ADR §6, `MAI_TRUSTED_PROXIES` set | transcript; compose port policy record | design-frozen |
| AUTH-S5 | unauthenticated SSE ⇒ `401` before any stream byte; authorized reconnect dedups via replay cursor | `02-auth-api`; transport + cursor producer `05-jobs-api`, consumer `05-web-jobs` | P02 gate (auth half); P05 gate (transport/cursor half) | persisted run with events | transcript; reconnect shows zero duplicates | design-frozen |
| AUTH-NT | negative-control matrix (no cookie, forged `X-Admin`, expired/revoked, setup-state privileged route, unenrolled `server_id`, changed host key, non-trusted `X-Forwarded-*`) | `02-auth-api` / `03-dispatcher` / `04-ssh-connect` per row | P02/P03/P04 gates | same fixture set | per-row expected outcomes table (ADR §9) | design-frozen |

## WS — windowed workspace (windowed ADR `e2cde76` §10–§11)

| ID | Scenario (definition) | Producer (owns test/entrypoint) | Verification gate | Fixtures / prerequisites | Expected evidence | Status |
|---|---|---|---|---|---|---|
| WS-PT1/NT1/NT2 | drag by title bar; interrupted gesture reverts; wrong-origin never drags | `01-window-frame` (chrome) + `01-window-drag` (pointer adapter) | P01 feature gate, assembled by `01-workspace-runtime`; P12 re-exercises | `01-web` real-browser tooling + `frontend/tests/fixtures/component-host.svelte`; `01-ui-tokens` values | real-browser pointer transcripts/video; geometry commit records | design-frozen |
| WS-PT2 | focus/bring-to-front, z-order, focus lands on title bar | `01-window-state` | P01 gate / `01-workspace-runtime` | two-window fixture | a11y tree + state transcript | design-frozen |
| WS-PT3/NT1 | resize with `min_geometry` clamp; interrupted resize clamps | `01-window-resize` | P01 gate | component host | geometry clamps recorded | design-frozen |
| WS-PT4 | minimize/restore via taskbar; content unmounts; `aria-pressed` | `01-window-presentation` | P01 gate | taskbar fixture | DOM/a11y transcript | design-frozen |
| WS-PT5 | close releases subscriptions; backend job continues; taskbar entry gone | `01-window-state` | P01 gate | panel with fake-event source (inert fixture) | subscription lifecycle record | design-frozen |
| WS-PT6/NT3/NT4 | same-browser restore; corrupt/offscreen/unknown-kind entries dropped with notice; bounds invariant ≥64×32 | `01-window-layout` + `01-window-responsive` | P01 gate; P12 viewport/zoom journey | stored-layout fixtures incl. hostile payloads (ADR §11 bad set) | restore transcripts; no dynamic-import attempts | design-frozen |
| WS-NT5 | global selector change never retargets open windows/pending ops/child jobs | `01-workspace-runtime` (assembly), scope logic `01-window-state` | P01 gate; re-tested with real jobs at P12 | two stored server-scope fixtures with different prompts/documents | scope-pinning transcript | design-frozen |
| WS-NT6 | close/minimize during running job; reopen reconnects to same `run_id`; same run in two windows submits once | `01-workspace-runtime` + `05-web-jobs` | P05/P12 gates | persisted run + SSE replay (EVT row below) | run-id identity + single-submission proof | design-frozen |
| WS-CHAT | real Chat+Job+Docs panels observe one persisted run; close/reopen without duplicate job/subscription | feature panel cards (P03–P11) + `01-workspace-runtime` | P12 multi-panel journey | live panels + persisted run | journey transcript | design-frozen |

## JOB — durable engine (job-engine ADR `bc8d264`, measured 2026-10-12)

| ID | Scenario (definition) | Producer (owns test/entrypoint) | Verification gate | Fixtures / prerequisites | Expected evidence | Status |
|---|---|---|---|---|---|---|
| JOB-EXP1–8 | checkpoint/replay (exp1), effect-before-checkpoint window (exp2), submit dedup (exp3), child trees + tree cancel (exp4a–c), schedule skip-missed (exp5), cancel point (exp6), pg_dump restore (exp7), Postgres outage (exp8) | `00-job-spike` probe `experiments/durable_job_probe.py` (per-experiment verdicts, exit 0/2) | regression gate: rerun on any DBOS upgrade or guard-rule change; P05 consumers re-verify guard rules at the P05 gate | disposable Postgres 18 fixture per runtime-environment ADR D1–D4 | verdict JSON + journals in `mai_evidence/00-job-spike/` | measured |
| JOB-GUARD | guard rules 1–10 hold in the adapter: verify-then-mutate steps, `outcome_unknown` on lost checkpoint, requested≠verified cancel, explicit tree cancel, ID-reuse conflict surface, skip-missed schedules, fire-time args, deliberate resume, single-dump restore, outage pause | `05-engine-adapter`, `05-run-lifecycle`, `05-cancel-recovery`, `05-schedules` | P05 feature gate (`05-verify`) | DBOS 3.2.0 + Postgres fixture; kill/restart harness | focused tests + kill-mid-step repro | design-frozen |

## EXEC — SSH execution and supervision (host-supervision ADR `1ce8181`, measured 2026-10-12)

| ID | Scenario (definition) | Producer (owns test/entrypoint) | Verification gate | Fixtures / prerequisites | Expected evidence | Status |
|---|---|---|---|---|---|---|
| SSH-EXP0–4B | preflight; unowned session-attached orphan (1a); wrapper-owned survival + identification (1b); verified cancellation + PID-reuse refusal (2); supervisor death ⇒ `outcome_unknown` (3); restart/recreate identity + host-key refusal (4a/4b) | `00-ssh-spike` probe `experiments/ssh_supervision_probe.py` (roles `preflight`/`exp1`–`exp4`/`cleanup`) | regression gate on probe/scheme change; P04 consumers re-verify at the P04 gate | `panubo/sshd` fixture container per ADR fixture rule; unique `mai-fxt-…` name; cleanup proof | verdict JSON + `commands.log`/`journal.log`/`verdict.txt` in `mai_evidence/00-ssh-spike/` | measured |
| SSH-SYSV1 | systemd transient-unit variant boundary-verified on a systemd-capable disposable target before load-bearing use (host rule 6) | `04-supervisor` | P04 feature gate | systemd-capable disposable container/VM; same probe roles | per-role verdicts; receipt-contract parity vs wrapper variant | design-frozen |
| SSH-PIN | changed host key on enrolled target ⇒ refuse, no silent re-trust; re-enrollment is explicit admin action | `04-ssh-connect` (target-level refusal measured in exp4b) | P04 gate | enrolled fixture, then recreated container | refusal transcript + re-enrollment audit record | design-frozen (target half measured) |
| SSH-CANCEL | requested-cancel flow: identity guard → TERM payload group → KILL escalation → ppid-closure empty ⇒ `cancelled`; package-manager critical-section accounting | `04-reconcile`, `04-supervisor` | P04 gate | wrapper-owned long-running fixture tree | receipt `events.log` + closure check transcript | design-frozen (core measured exp2) |
| EXEC-JOURNEY | real browser executes benign fixture command, password challenge, large output, lost connection; no fake succeeded status | `04-command-output`, `04-password-channel`, UI `05-web-execution` | P05 gate (`05-verify`) | fixture sshd + bounded output payload | DOM/network evidence without secret values | design-frozen |

## KB — knowledge/memory stack (knowledge-packaging ADR `5d6e0a1` §5)

| ID | Scenario (definition) | Producer (owns test/entrypoint) | Verification gate | Fixtures / prerequisites | Expected evidence | Status |
|---|---|---|---|---|---|---|
| KB-S1 | SearXNG serves JSON (`search.formats` includes `json`; missing ⇒ 403) | `07-searxng` settings; round-trip owner `00-verify` | P00 readiness gate; P07 gate re-check | disposable compose stack, internal names per ADR | curl transcript with `results[0].title` | design-frozen |
| KB-H1 | Hindsight health/version incl. DB reachability | `00-verify`; adapter owner `11-memory-adapter` | P00 readiness; P11 gate | `hindsight` service + `hindsight` DB | health/version transcripts | design-frozen |
| KB-H2 | retain→recall round trip; record LLM-unreachable and pgvector-missing failure modes; retain failure never reported as success | `00-verify`; `11-memory-adapter` bank scoping | P00 readiness; P11 gate | bank `mai-smoke` (disposable) | retain/recall payloads + failure-mode notes | design-frozen |
| KB-E1 | embeddings round trip ⇒ dims 1536; never pointed at `/chat/completions` | `00-verify`; consumer owners `07-vector-schema` | P00 readiness; P07 gate | configured OpenAI-compatible endpoint via `.env` | jq dims output | design-frozen |
| KB-V1 | pgvector round trip: `vector(1536)`, `vector_dims`=1536, cosine distance finite; `pg_trgm` present | `00-verify`; platform Postgres image decision | P00 readiness; P01/P02 platform gate | pgvector-enabled Postgres container | psql transcript | design-frozen |
| KB-FILTER | retrieval results honor server filter at the storage boundary; stale chunks removed on source update/delete | `07-retrieval`, `07-chunk-index` | P07 gate | two fixture servers with distinct corpora | per-server result sets + deletion propagation | design-frozen |
| KB-BANKS | Hindsight bank-per-server + explicit shared banks enforced in adapter; cross-server recall denied | `11-memory-adapter` | P11 gate | two server banks + one shared | denied cross-bank recall transcript | design-frozen |

## CT — contract-level runtime routes/commands (contracts.md C01–C10)

| ID | Scenario (definition) | Producer (owns test/entrypoint) | Verification gate | Fixtures / prerequisites | Expected evidence | Status |
|---|---|---|---|---|---|---|
| FILE-CRASH | W1–W4 reconciliation: intent-then-file-then-projection crash recovery, tombstone/purge, no invented sync | `02-file-store`, `02-profile-projection` | P02 gate | data-root fixture + killable projection writer | per-window outcome transcripts | tabletop-done |
| APPROVAL-PAYLOAD | altered payload hash / target / expired / policy-revision-changed ⇒ deny + new approval required; effect counter unchanged on denial | `03-approval-api`, `03-dispatcher` | P03 gate | fixture capability with effect counter | denial transcripts + unchanged counter | tabletop-done |
| GRANT-INTERSECT | child grants = intersection; recheck immediately before privileged effect; `deny` precedence | `03-policy`, `06-child-runs` | P03/P06 gates | parent+child grant fixtures | effect-denial transcripts | tabletop-done |
| CROSS-SERVER | two server-scoped fixtures never exchange prompt/logs/credential challenges/app selection/retrieval cache; context binds `server_id` | `02-profile-schema` (two-server IDs), `06-context`, `07-retrieval`, `11-memory-adapter` per layer | P02/P06/P07/P11 gates | two distinct server fixtures | per-layer isolation transcripts | tabletop-done |
| JOB-SUBMIT | authenticated HTTP creates job, idempotent duplicate submit, disconnect leaves work running, reconnect from cursor, cancel with scope check, unauthorized subscription denied | `05-jobs-api` | P05 gate (`05-verify`) | worker + DB + fixture run | HTTP/SSE transcripts + DB states | design-frozen |
| EVT-REPLAY | SSE replay-cursor reconnect deduplicates; transport + wire format settled here | `05-jobs-api` producer; `05-web-jobs` consumer | P05 gate | persisted event log | reconnect transcript, zero duplicates | design-frozen (open C08 item — closes it) |
| LOCK-FENCE | real lock contention/fencing on `server_id`+resource key; ownership-checked heartbeat/release; no duplicate mutation after fence | `05-resource-locks` | P05 gate | two concurrent workers, one fixture target | contention timeline + single-effect proof | design-frozen |
| BUDGET-CAP | child cap cannot exceed root remaining budget; concurrency-safe reservation; no worker-slot deadlock from child waits | `06-delegation-budgets` | P06 gate | delegation tree fixture | budget ledger + tree completion | design-frozen |
| AGT-PROVIDER | real configured provider + tool-call loop; resumed run; deterministic fake LLM limited to plumbing tests | `06-agent-run`, `06-tool-bridge` | P06 gate | live provider via gateway config (credentials via `.env`) | tool-loop + resume transcripts | design-frozen |
| COLLECT-NO-LLM | deterministic collectors/notifications proceed while LLM unavailable; notifications dedup-keyed with delivery status | `10-cron`, `10-process`, `10-notifications` | P10 gate | collector fixture schedule | collection records + dedup audit | design-frozen |
| BACKUP-RESTORE | single `pg_dump` covers stack incl. DBOS tables + `hindsight` DB; restore-to-fresh + relaunch ⇒ every observable effect exactly once (job exp7 pattern) | `12-backup-restore` | P12 gate | running stack fixture | restore transcript + post-restore run identities | design-frozen (engine half measured exp7) |
| E2E-ROUTE | end-to-end execution route exactly as SERVICES §"End-to-end execution route": no bypass route in web route/MCP bridge/cron/child agent | `12-end-to-end` assembler | P12 gate | full stack + fixture target | journey transcript incl. denial cases | design-frozen |

## Coverage statement (acceptance 2)

Every downstream planned runtime route/command named in `contracts.md` maps to a
registry row: auth boundary (AUTH-*), privileged execution (EXEC-*, EXEC-JOURNEY),
jobs/schedules/events (JOB-*, JOB-SUBMIT, EVT-REPLAY, LOCK-FENCE), approvals/grants
(APPROVAL-PAYLOAD, GRANT-INTERSECT), canonical files/projections (FILE-CRASH),
retrieval/memory (KB-*), delegation (BUDGET-CAP, AGT-PROVIDER), collectors/notifications
(COLLECT-NO-LLM), backup/restore (BACKUP-RESTORE), windowed UI (WS-*), final route
(E2E-ROUTE). Tabletop-only rows carry their runtime owner and gate — none is
claimed as runtime-PASS here.

Rules binding on all rows:

1. A `design-frozen` command is verified by its producer before first use; no
   consumer may treat it as executed until its evidence exists.
2. Fixtures follow runtime-environment ADR D1–D5 (namespace, budget, cleanup proof).
3. Scenario content lives in the cited ADR sections; if an ADR changes, its rows here
   are re-frozen in the same reconciliation motion (contracts.md §Change control).
4. One producer test may cover several rows; no wrapper-per-card proliferation
   (`mai_TEST_POLICY.md` §"Feature runtime coverage").

## Provenance

Registry assembled 2026-10-12 by `mai-00-contracts` from the six frozen P00 ADRs
(SHAs in `contracts.md`). Ownership names are manifest task names
(`mai_TASKS.json`/card briefs); gates are the phase feature gates per
`mai_EXECUTION.md` §4/§6. Author coverage/integrity checks:
`mai_evidence/00-contracts/01-inputs-and-checks.md`.

---

## RV2 — runtime v2 live scenarios (rv campaign, 2026-11-01)

Contract source: [runtime-v2.md](runtime-v2.md) §12 (campaign task `contracts`).
The rows below were frozen as `design-frozen` (definitions + fixtures) and are
now `measured` by the `acceptance` campaign gate: executed on the integrated
campaign tip by `backend/tests/test_runtime_v2_acceptance.py` (real provider +
real disposable Postgres 18 fixture + real disposable sshd target + real
workers; scripted models only for labelled deterministic fault injection) with
evidence under `plans/runtime-v2/rv_evidence/acceptance/` (journey metrics in
`journey_metrics.json`, per-scenario verdicts in `verdict_matrix.json`,
`summary.md`, `verification_receipt.json`). `RV2-UI-A11Y` is executed by the
Playwright suites (`tests/activity-ui-v2.spec.ts tests/delegation.spec.ts
tests/api-client.spec.ts`, one worker + bounded Node heap) and re-recorded by
the same acceptance run. All fixtures follow runtime-environment ADR D1–D5
(disposable targets, namespace `mai-fxt-…`, cleanup proof, no secrets in
evidence). `RV2-DEPLOY-DRAIN` stays `design-frozen`: its verification gate is
the deploy gate (`rv-deploy`), not the acceptance gate.

| ID | Scenario (definition) | Producer (owns test/entrypoint) | Verification gate | Fixtures / prerequisites | Expected evidence | Status |
|---|---|---|---|---|---|---|
| RV2-JOURNEY | One disposable authorized request completes inspect → act → verify → report in ONE submission through the actual v2 chat API/UI with a real provider: no intake call, no second user turn, receipt-backed response | `acceptance` (journey runner); `conversation-api` (route); `action-runtime` (`agent.run_v2` handler) | campaign acceptance gate | blocking preflight-designated disposable mutation-capable target; real provider via configured gateway (credentials via `.env`); real persistence | journey transcript + receipts; zero intake events on the run | measured |
| RV2-DENY | Research-only server: direct AND delegation-carried mutation attempts produce no effect; sanitized denial is journaled and visible | `policy-delegation` (policy projection); `acceptance` (negative journeys) | campaign acceptance gate | second disposable target configured `research_only` via `GrantConfigStore`; policy revision fixture | denial transcripts + sanitized decision events; zero effects | measured |
| RV2-DELEGATE | Real parent/child v2 runs over TWO scoped targets: child target/capability/budget subsets hold; ordered activity; parent aggregates child receipts, not claims | `policy-delegation` (`ChildSpecV2` payload v2); `acceptance` (journey) | campaign acceptance gate | two disposable Compose/SSH targets; objective with operator-expanded scope | delegation tree events + child receipts + aggregate verification | measured |
| RV2-APPROVAL-RESTART | Approval-deferred effect survives a real worker restart; the approved operation executes its EXACT payload exactly once via the payload-bound resume | `action-runtime` (resume seam) | campaign acceptance gate | disposable target; kill/restart harness; approval fixture bound to payload hash | restart + single-effect proof (receipt identities); no duplicate mutation | measured |
| RV2-REPLAY | Crash/restart replay keeps v2 and legacy durability: settled checkpoints replay, parked waits re-park, no duplicate effects | `acceptance` (harness); `action-runtime` | campaign acceptance gate | disposable Postgres; seeded v2 + legacy runs | replay transcripts; event dedup on reconnect | measured |
| RV2-UNKNOWN | An effect whose receipt is lost becomes `outcome_unknown`, pauses in `reconciling`, and NEVER auto-resumes into execution until classified | `action-runtime` + `mai.jobs.recovery` integration; `acceptance` | campaign acceptance gate | fixture target with induced lost receipt | reconciliation transcript; explicit unknown until classified | measured |
| RV2-UI-A11Y | Real browser at 360×640 AND desktop, `prefers-reduced-motion: reduce`, keyboard-only: expand/collapse every event detail, steer, pause/cancel, approve, navigate the delegation tree; accessible names/roles, visible focus, no horizontal overflow, no focus theft during streamed events, no animation | `activity-ui` | activity-ui gate (`acceptance` re-runs journey-level) | built frontend + API; exclusive browser port; live v2 run fixture | a11y tree + interaction transcripts; overflow/focus assertions | measured |
| RV2-LEGACY | Seed real legacy conversation/run/task fixtures; migrate, read, and drain: legacy history stays truthful and readable; unsettled legacy runs drain or replay by their FROZEN `agent.run` worker; never reinterpreted as v2 | `journal-store` (migrations); `acceptance` (seed/read/drain) | campaign acceptance gate | disposable migrated Postgres; real legacy row fixtures; drain inventory from preflight | read-compatibility transcripts + drain ledger; zero history rewrites | measured |
| RV2-DEPLOY-DRAIN | Production deployment gates on a drained legacy active-run inventory; health/readiness checks, v2 smoke, legacy-read compatibility and a rehearsed rollback (application vs irreversible-migration scope distinguished) | `deploy` | deploy gate | production path per plan; pre-deploy backup where applicable; rollback rehearsal record | deploy receipt: revision, health, smoke, drain status, rollback trigger/result | design-frozen |

### RV2 coverage statement (runtime-v2.md §12)

Every acceptance scenario named in runtime-v2.md maps to a row above with a
producer and fixtures: real provider journey (RV2-JOURNEY), research-only
denial (RV2-DENY), multi-server delegation (RV2-DELEGATE), approval +
restart (RV2-APPROVAL-RESTART), restart/replay durability (RV2-REPLAY),
unknown effects (RV2-UNKNOWN), UI a11y at 360×640 + desktop (RV2-UI-A11Y),
legacy seed/read/drain (RV2-LEGACY), deployment/drain (RV2-DEPLOY-DRAIN).
Required kinds are covered: real provider (RV2-JOURNEY), disposable targets
(RV2-JOURNEY, RV2-DENY, RV2-DELEGATE, RV2-APPROVAL-RESTART, RV2-UNKNOWN,
RV2-LEGACY), browser/a11y (RV2-UI-A11Y), restart (RV2-APPROVAL-RESTART,
RV2-REPLAY), unknown effects (RV2-UNKNOWN), deployment/drain
(RV2-DEPLOY-DRAIN), delegation (RV2-DELEGATE), legacy compatibility
(RV2-LEGACY). One producer test may cover several rows; the
registry rules 1–4 above bind RV2 rows identically.
