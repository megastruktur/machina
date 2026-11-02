# Runtime v2 — action-first single-operator agent contracts (rv campaign)

- **Campaign:** `runtime-v2`, task `contracts` (branch `megastruktur/rv-contracts`)
- **Status:** FROZEN contract slice. This document is the authoritative Runtime v2
  contract for the later campaign slices (`journal-store`, `action-runtime`,
  `policy-delegation`, `conversation-api`, `activity-ui`, `acceptance`, `deploy`).
  It is documentation-only: no implementation handler or production migration
  belongs to this slice.
- **Converged inputs (exact provenance):** `plans/runtime-v2/rv_INTENT.md`
  (SHA-256 `d7245204ed1b4b8c8123344493bf981fb2ac75e48886507a48939ee65d00cf59`) and
  `plans/runtime-v2/rv_RESEARCH.md` (SHA-256
  `328f364538ca1d9825b825fb2be4d5504fde25e4582d9afe700b2eb1f9fdd22b`), verdict
  **CONVERGED** round 2 (`inquisitor:t_5c830ebb`), recorded in `rv_PLAN.md`.
  Repository: `/home/megastruktur/projects/dev/machina` (ACD `rv` campaign).
- **Precedence:** the frozen P00 contracts ([contracts.md](contracts.md) C01–C11)
  and the act addendum ([agent-task-continuity.md](agent-task-continuity.md))
  remain binding except where §"Supersessions" below explicitly supersedes a rule
  for v2 conversations. Everything not superseded carries over unchanged:
  dispatcher-only authorization, approval payload binding, effect taxonomy and
  receipts, single-admin authority, DBOS guard rules, the frozen C05 run
  lifecycle, pagination, secret projection and lock/fence semantics.
- **Naming rule:** names in the two "machine-verified pin" sections below are
  EXISTING repository symbols, verified by
  `backend/tests/test_runtime_v2_contracts.py` at freeze. Names inside the
  `<!-- design-targets -->` fence are frozen design targets for their named
  producer slice and are NOT repo symbols yet — a producer may not rename them
  without reconciling this document. Unmarked prose names follow the same rule as
  [contracts.md](contracts.md) §"Naming rule".

## 1. Supersessions and preserved invariants

### 1.1 Superseded rules (complete list)

| # | Superseded rule | Where it lived | Runtime v2 rule |
|---|---|---|---|
| S1 | Mandatory structured intake per accepted user turn (`TurnInterpretation` planner call, one-repair loop) as the gate before any working-model round | contracts.md act addendum item 3; agent-task-continuity.md §4; the legacy handler's intake seam (`backend/src/mai/agents/run.py`, `backend/src/mai/agents/intake.py`) | A v2 conversation submission goes directly to the working model. No intake planner call, no structured turn union, no mandatory todo. Structured intake remains the LEGACY handler's behavior and is retained unchanged there; it is never run for a v2 conversation. |
| S2 | One-server-only child scope: `ChildSpec` deliberately carries no server field ("a child runs on its parent's server, full stop") | `backend/src/mai/agents/delegate.py` (`ChildSpec`); contracts.md C05/C06 delegation seams | Delegation payload version 2 carries explicit per-child `server_ids` subsets of the parent objective scope (§7). The v1 single-server payload stays frozen for replay. |
| S3 | The task/plan projection as the only first-class progress surface (intake-proposed todo as the working state) | agent-task-continuity.md §4/§6 | The v2 work journal (§8) is the trusted progress projection; its checklist is optional and advisory. The legacy task vocabulary stays authoritative for legacy conversations and for evidence-bound completion semantics. |
| S4 | A generic "resume" affordance | superseded before it ever shipped (rv_RESEARCH §"UI live operations view"; critique RV-08) | Resume is state-specific per §9; unknown effects never auto-resume into execution. |
| S5 | `agent.intake_started` / `agent.intake_interpreted` / `agent.intake_timed_out` / `agent.conversation_history` as first-class UI states for the current conversation surface | `backend/src/mai/agents/run.py` event constants | v2 UI consumes the canonical activity families (§5); legacy-history rendering is retained. Those four types are legacy-handler-owned and never emitted on a v2 run. |

No other rule is superseded. §C03 rule 3 (child grants are an intersection),
§C03 rule 4 (approval binding), §C05 rules 1–8, §C06 rule 4 (dispatcher is the
one authorization boundary; deferred tools are not), §C08 (single-admin session,
origin/CSRF, SSE replay) and the act addendum §1 identity/lease rules are
**preserved verbatim** for v2.

### 1.2 Threats this contract answers (from rv_RESEARCH §"Security analysis")

Scope escalation (child subsets only, §7), forged approval (payload-bound
approvals unchanged), event secret leakage (§5.3 redaction), replay duplication
(idempotency + state-specific resume, §9), legacy/history ambiguity (§10:
frozen legacy replay, read-only history, no v2 reinterpretation).

## 2. v2 handler identity and versioned intent

1. **Handler name (frozen):** `agent.run_v2`. It satisfies the engine's frozen
   dotted-slug handler-name grammar (`mai.jobs.engine` handler-name regex) and is
   registered through the ONE registration seam
   (`mai.jobs.engine.register_job_handler`) — the same trampoline
   (`run_workflow`) resolves it at execution time. Worker processes import the
   producer module via the existing `MAI_WORKER_HANDLERS` import discipline
   (`backend/src/mai/jobs/worker.py`); no second engine, queue or trampoline exists.
2. **Versioned intent:** the v2 run payload document carries
   `payload_version` (design target: starts at `1` in the
   `action-runtime` slice). Parse rules follow the legacy discipline: unknown
   fields reject, missing fields reject, and a payload whose
   `payload_version` is not recognized fails closed (structured, value-free
   rejection — never a narrower guess).
3. **Exclusive lease:** a v2 run and a legacy `agent.run` can never own the same
   conversation lease at the same time (act addendum §1 writer lease, C05 rule 6).
   A conversation created as v2 stays v2; a legacy conversation is never
   converted in flight (§10).
4. **Deterministic identity:** `run_id` remains the C05 durable engine attempt
   (idempotency key = deterministic workflow ID). Submit dedup semantics
   (identical intent re-arms the same run; changed content under the same key is
   a structured conflict) carry over unchanged.

## 3. Objectives, scopes and runs

1. **`objective_id`** joins the C01 opaque-UUID identity set. An objective is the
   explicit goal episode: the verbatim original request + operator constraints
   (provenance-carrying, never model-reworded), a status, and the **authorized
   server scope**: an explicit, operator-owned set of `server_id`s.
2. **Scope expansion is operator-only.** No agent, child or model may add a
   server to an objective's scope. The scope change is recorded as an
   `objective.*` event (§5) with the operator actor kind.
3. **Run ↔ objective binding:** every v2 run carries its `objective_id` and one
   **initiating `server_id`** (the UI-compatibility context, a member of the
   objective scope at submit time). Every effect still carries exactly one
   explicit target `server_id` (C01 rule 2 discipline); independent reads may
   fan out, but mutations on overlapping targets use the existing resource locks
   and deterministic sequencing (C05 rule 6).
4. **Conversation binding:** a conversation links to its objective episodes; the
   legacy rule "one active task per conversation" is superseded for v2 by the
   objective model (S3), while legacy conversations keep their existing
   task/episode semantics read-only.
5. **Objective scope grants capabilities, not authority:** exposure is computed
   per §6; nothing here widens grants, approvals or policy.

## 4. Actor provenance

Audit/telemetry records carry an explicit actor kind from the frozen vocabulary:

| Actor kind | Meaning |
|---|---|
| `operator` | the authenticated single human administrator session (the only human authority; auth-and-origin.md S9) |
| `agent` | a top-level v2 run executing for the operator |
| `child_agent` | a delegated child run (an agent identity, never a user, never an approver) |
| `automation` | deterministic schedules/collectors/recovery paths (no model involvement) |

Actor kinds are provenance labels only. They create no new principal, no user
table, no RBAC (single-operator non-goal), and a `child_agent` can never hold or
grant a human approval (§C03 rule 4 unchanged; model-origin proposals never
supersede USER-origin constraints).

## 5. Canonical activity ledger

One canonical ordered activity stream per run — the existing per-run event log
with the frozen C05 rule 4 envelope (`{"seq","run_id","ts","type","v","data"}`,
monotonic replayable `seq`, versioned payloads). The live UI, history
reconstruction and the work journal (§8) are PROJECTIONS of these records; no
parallel execution engine, no second ledger.

### 5.1 Event families (frozen)

| Family | Owner | Types |
|---|---|---|
| `run.*` | existing facade/recovery modules | existing: `run.submitted`, `run.transition`, `run.recovery_parked` |
| `chat.*` | `conversation-api` slice | existing: `chat.submitted`, `chat.steered`, `chat.renamed` (steer/pause/cancel control requests ride this family) |
| `objective.*` | `journal-store` slice | NEW family for objective lifecycle, journal and checklist projections (design targets §14): creation, operator scope changes, status, journal/checklist updates, settled outcome |
| `agent.*` working loop | `action-runtime` slice | REUSED verbatim: `agent.started`, `agent.context_assembled`, `agent.model_round`, `agent.output_delta`, `agent.tool_requested`, `agent.tool_result`, `agent.awaiting_reconciliation`, `agent.completed`, `agent.failed`; NEW v2-only types may be added in this family (additive, dotted-slug grammar) for v2-specific boundaries; FORBIDDEN on v2 runs: the four legacy intake types (S5) |
| delegation | existing `agent.delegate` producer | existing: `agent.delegation_planned`, `agent.delegation_wait`, `agent.children_resolved`, `agent.delegation_completed` |
| budget | existing budget ledger (`mai.agents.budgets`) | existing: `agent.budget_reserved`, `agent.budget_released`, `agent.budget_charged` |

Rules: event writes can never contradict dispatch/facade state; types are
value-free and carry identities/sanitized references, never task prose or
secrets; a producer may add types only inside its own family, additively, with
versioned payloads — renaming or reusing a type with a new meaning requires
contract reconciliation.

### 5.2 Pagination bounds (AGENTS.md mandatory invariant)

Every collection view over the ledger and journal is a bounded page envelope
with `DEFAULT_PAGE_LIMIT = 50`, `MAX_PAGE_LIMIT = 200` (frozen in
`backend/src/mai/contracts/conversations.py`), fields `limit`, `offset`,
`total`, `has_more`, deterministic ordering, `COUNT(*)` under the same filter,
and derived `has_more` (`offset + len(items) < total`, never fabricated). SSE
activity streams stay authenticated, origin-checked, replay-cursor based and
deduplicated on reconnect (C08 rule 7); a replay is read-only.

### 5.3 Redaction bounds

- Secret values NEVER enter event payloads, model/job payloads or the journal
  (C05 rule 3, C03 rule 6, act addendum §2). Credentials resolve by reference.
- Bounded sanitized projections: the frozen stream budgets from the legacy
  executor carry over — `STREAM_DELTA_MAX_CHARS = 4096` for output deltas and
  `STREAM_TOOL_DETAIL_MAX_CHARS = 32768` for tool detail
  (`backend/src/mai/agents/run.py` pins).
- Secret-like tool output is projected (redaction markers, safe
  `content_ref`s) BEFORE persistence; redacted content makes no
  textual-fidelity claim. A denied tool call still emits a sanitized decision
  event — exposure never silences the ledger.

## 6. Capability exposure (default granted-on-objective)

The user's full-list default is: **every capability granted on at least one
server in the objective's scope**, filtered per target:

```
visible(target) = granted_on_objective_servers − server_denies(target)
                  − objective_denies − parent_denies (for children)
```

- Exposure is a UI/UX projection, never an authorization decision: every call is
  revalidated by the ONE dispatcher (`backend/src/mai/capabilities/dispatch.py`,
  `CapabilityDispatcher`) for its exact target and payload (C06 rule 4);
  `deny` wins everywhere; unknown effect classes fail closed.
- Ungranted plugins are not advertised to create predictable denials; a direct
  denied call is still journaled (§5.3).
- **`research_only`** is a named server-policy template that projects into the
  existing versioned policy config (`<data_root>/policy/config.json` +
  `config.meta.json`, managed by `backend/src/mai/policy/config.py`
  `GrantConfigStore` with `expected_revision` CAS and a single monotonic policy
  revision — `backend/src/mai/policy/evaluate.py` `Policy`). It is a
  deterministic projection into explicit grant/deny sets, NOT a second policy
  engine. Approvals and dispatch decisions bind the policy revision (C03 rule 4).

## 7. Delegation v2 (no alternate engine)

1. **One delegation engine.** v2 delegation reuses the existing
   `agent.delegate` handler, `DelegationExecutor`, `plan_children`, the
   `waiting_children` lifecycle edges, budgets and the delegation UI
   (`frontend/src/lib/components/DelegationTree.svelte`). No second delegation
   engine, no parallel child-run machinery.
2. **Versioned payload migration.** The single-server `ChildSpec` (no server
   field) and its payload (`DELEGATION_PAYLOAD_VERSION = 1`) stay frozen forever
   for replay of legacy parents. V2 parents emit **payload version 2** whose
   child specification (design target `ChildSpecV2`, `policy-delegation` +
   `action-runtime` slices) adds:
   - `server_ids`: non-empty explicit subset of the parent's effective objective
     server scope (a child's set ⊆ parent's effective set; C03 rule 3
     intersection unchanged);
   - `capabilities`: subset of the parent's effective grants (request only —
     effective grants remain the intersection with the parent's granted set);
     budget bounds: a child's reservation is bounded by the parent's remaining
     budget in the existing ledger vocabulary (`backend/src/mai/agents/budgets.py`
     pin table: `MAX_ACTIVE_RUNS`, `MAX_TOTAL_STEPS`, `MAX_TOTAL_TOKENS`; reserve/
     release/charge events). A child cap can never exceed the root's remaining
     budget (C05 rule 7).
3. **Caps unchanged:** `MAX_CHILDREN_PER_RUN = 8`, `MAX_DELEGATION_DEPTH = 4`
   (frozen constants of the delegation module).
4. **Child runs select `agent.run_v2`** when the parent is v2; a v1 payload
   parent keeps producing legacy `agent.run` children. Children of a v2 parent
   can never widen target, capability or budget scope; the operator expands the
   objective instead.
5. **Aggregation:** the parent aggregates child RECEIPTS, never child claims;
   tree cancellation, wait budgets and non-deadlocking child waits are the
   existing engine seams.

## 8. Work journal, optional checklist, evidence-bound completion

1. **Journal = projection.** The journal projects trusted records only:
   verbatim goal/constraints with provenance, action attempts with sanitized
   arguments, dispatcher decisions, operation IDs, receipts, resource-lock and
   process state, artifacts, verification results, delegation links + budget
   state, model lifecycle/retry/compaction metadata. It is not a second
   execution engine and never contradicts the activity ledger.
2. **Optional checklist (advisory).** The agent may maintain a bounded
   checklist projection in the `objective.*` family. A malformed checklist write
   is bookkeeping trouble (a visible, bounded observation) — never conversation
   rejection and never evidence. No todo is mandatory; no checklist tick grants
   authority or completes anything.
3. **Evidence-bound completion.** Completion requires postconditions covered by
   passed results bound to evidence — the existing
   `backend/src/mai/contracts/tasks.py` `TaskCompletionVerdict` discipline,
   including `completion_missing_evidence` — carries over to v2. Model prose
   never inflates evidence; `domain_outcome` stays honestly `unknown` for
   arbitrary shell work; unknowns stay explicit.
4. **Journal/checklist write failures cannot turn successful effects into
   "nothing happened"** (rv_RESEARCH §"Failure and recovery"): the effect's
   durable records (receipts, checkpoints) remain the authority; the projection
   catches up honestly.

## 9. Waiting, paused, resume, reconciliation (state-specific)

| Parked state | Resume behavior (frozen) |
|---|---|
| identical resubmission | re-arms the SAME accepted run (idempotency key = deterministic workflow ID; changed content is a structured conflict) — not a new run |
| `waiting_approval` | resumes ONLY the payload-bound approved operation (C03 rule 4 binding: payload hash/target/scope/policy revision/expiry); the one-shot resume bridge is the existing `dispatch_once(approval_id=…)` seam of `backend/src/mai/agents/tools.py` — worker-side, never generic HTTP re-execution |
| `waiting_children` | resumes through the settled-child/reconciliation producers (the delegation module's wake seam and `backend/src/mai/jobs/recovery.py`), reading engine truth for child terminality |
| `reconciling` | resumes ONLY after explicit effect classification (the recovery module's reconcile flow); unknown effects NEVER auto-resume into execution |
| `paused` (operator control) | resumes at the NEXT safe model/tool boundary; steering and scope are rechecked before each new tool effect (act addendum §3 carries over verbatim) |
| unknown effect | never resumes blindly: reconciliation classifies first (outcome_unknown semantics, C04 rule 5 / job-engine guard rule 2) |

Approval waits stay durable (never busy loops); a restart replays settled
checkpoints and re-parks waits without duplicating effects.

## 10. Legacy retention, drain and read compatibility

1. **Frozen legacy replay.** The `agent.run` handler identity and its
   checkpoint sequence (intake step, model/tool rounds) are FROZEN while any
   unsettled legacy run exists: no reshaping, no rename, no second intake
   planner over the same run. Its registration in the engine handler map is a
   verified pin (§13) for the whole drain window.
2. **Drain policy.** Deployment retires the legacy worker/handler only after
   every legacy unsettled run reached a settled terminal state, or retains the
   legacy worker until then. An in-flight legacy run is never resumed as v2 and
   never converted. The `deploy` slice owns drain verification (scenario
   RV2-DEPLOY-DRAIN in [runtime-scenarios.md](runtime-scenarios.md)).
3. **Read compatibility forever.** Legacy conversations, transcripts, task and
   run history remain readable through the existing read surfaces
   (`backend/src/mai/history_store.py`, the conversation read routes of
   `backend/src/mai/api/conversations.py`): read-only, no history rewrite, no
   fake v2 achievements, `legacy_import`/`history_completeness` semantics
   unchanged (agent-task-continuity.md §8).
4. **Contract/pin consumers updated in this slice:** contracts.md gains the
   runtime-v2 addendum; agent-task-continuity.md marks structured intake as
   legacy-only and points v2 conversations at this document;
   runtime-scenarios.md registers the RV2 rows. The dbos/engine dependency pins
   and AGT-PROVIDER rows are unchanged (existing dependency tests keep
   passing).

## 11. API route ownership for later slices

Routes are additive during migration; all collection routes are paginated
(§5.2); every mutating route crosses the C08 session→origin→CSRF boundary; the
generated client schema is regenerated by the owning slice
(`frontend/src/lib/api/schema.generated.ts`, command documented in
`frontend/src/lib/api/client.ts`).

| Slice | API/persistence ownership |
|---|---|
| `journal-store` | Objective/journal/checklist persistence: the Alembic migration chain for objective, journal and checklist tables (ONE migration owner; later tasks consume the registered chain without renumbering); the `objective.*` event family writer; routes `POST /api/v1/objectives`, `GET /api/v1/objectives`, `GET /api/v1/objectives/{objective_id}`, `GET /api/v1/objectives/{objective_id}/journal`, `GET/PUT /api/v1/objectives/{objective_id}/checklist` (paginated, revision-CAS where mutable) |
| `action-runtime` | The `agent.run_v2` handler + runtime modules; worker registration via `MAI_WORKER_HANDLERS`; the v2 working-loop event emissions (§5.1 `agent.*` reuse); approval one-shot resume wiring (§9); unknown-effect reconciliation integration |
| `policy-delegation` | `research_only` template projection into the versioned policy config (`GrantConfigStore`); exposure projection consumed by tool descriptors; delegation payload version 2 (`ChildSpecV2`) in the ONE delegation engine |
| `conversation-api` | v2 chat/conversation routes and dispositions (v2 submission, steer/pause/cancel/approve HTTP), the activity SSE surface, legacy read compatibility of those routes, and the regenerated typed client/schema |
| `activity-ui` | Live activity UI (concise narrative + complete expandable ledger), delegation tree updates, state-specific resume controls per §9, wire-honest loading/empty/error states, a11y/mobile behavior |
| `acceptance` | Campaign-wide live-runtime journeys and fixtures: RV2 rows in [runtime-scenarios.md](runtime-scenarios.md), exclusive ports/Compose projects allocated by the coordinator |
| `deploy` | Production deployment, legacy drain verification, health/readiness + rollback rehearsal receipts (RV2-DEPLOY-DRAIN) |

## 12. Acceptance scenarios (registry)

All live-runtime scenarios are registered in
[runtime-scenarios.md](runtime-scenarios.md) §"RV2 — runtime v2 live scenarios"
with producer, gate, fixtures and expected evidence: real provider journey
(RV2-JOURNEY), research-only denial (RV2-DENY), multi-server delegation
(RV2-DELEGATE), approval + restart (RV2-APPROVAL-RESTART), restart/replay
durability (RV2-REPLAY), unknown effects (RV2-UNKNOWN), UI a11y at 360×640 +
desktop (RV2-UI-A11Y), legacy seed/read/drain (RV2-LEGACY), deployment/drain
(RV2-DEPLOY-DRAIN). `design-frozen` here; unit/schema checks alone are never
runtime acceptance for the later slices.

## 13. Verified tree pins (machine-verified)

Every path below EXISTS in this tree at freeze; every symbol resolves; every
frozen value matches. `backend/tests/test_runtime_v2_contracts.py` fails the
suite if any of these drifts.

| Path | Role in this contract |
|---|---|
| `backend/src/mai/jobs/engine.py` | the ONE durable trampoline + `register_job_handler` seam + handler-name grammar |
| `backend/src/mai/jobs/worker.py` | worker entrypoint; `MAI_WORKER_HANDLERS` import discipline; default agent-runtime wiring |
| `backend/src/mai/jobs/recovery.py` | reconciliation owner (`reconciling` resume semantics) |
| `backend/src/mai/agents/run.py` | legacy handler + stream redaction bounds + legacy event constants |
| `backend/src/mai/agents/delegate.py` | the ONE delegation engine (`ChildSpec`, `DelegationExecutor`, `plan_children`, caps) |
| `backend/src/mai/agents/budgets.py` | budget ledger vocabulary + events |
| `backend/src/mai/agents/tools.py` | `CapabilityToolBridge` + the `dispatch_once` approval resume seam |
| `backend/src/mai/agents/intake.py` | legacy structured-intake planner (retained, legacy-only) |
| `backend/src/mai/capabilities/dispatch.py` | the ONE dispatcher (authorization boundary) |
| `backend/src/mai/policy/config.py` | `GrantConfigStore` (versioned policy config home) |
| `backend/src/mai/policy/evaluate.py` | `Policy` revision binding, `ExecutionContext`, `ParentScope` |
| `backend/src/mai/contracts/conversations.py` | pagination bounds + `TurnInterpretation` legacy union |
| `backend/src/mai/contracts/jobs.py` | frozen `RunState` lifecycle vocabulary |
| `backend/src/mai/contracts/tasks.py` | `TaskCompletionVerdict` (evidence-bound completion) |
| `backend/src/mai/history_store.py` | legacy history read surface |
| `backend/src/mai/api/chat.py` | chat surface + `chat.*` events |
| `backend/src/mai/api/conversations.py` | canonical conversation read surface (legacy compatibility) |
| `backend/src/mai/api/jobs.py` | `run.*` facade events + paginated run/conversation listings |
| `frontend/src/lib/components/DelegationTree.svelte` | delegation UI seam (extended, not replaced) |
| `frontend/src/lib/panels/ChatPanel.svelte` | chat panel seam (activity UI producer) |
| `frontend/src/lib/api/client.ts` | typed client + schema regeneration command |
| `frontend/src/lib/api/schema.generated.ts` | generated schema (owned by `conversation-api`) |
| `docs/architecture/contracts.md` | frozen P00 contracts + act addendum |
| `docs/architecture/agent-task-continuity.md` | act addendum (identity, lease, intake) |
| `docs/architecture/runtime-scenarios.md` | scenario registry (RV2 rows) |
| `docs/architecture/auth-and-origin.md` | single-admin authority (S9) |
| `docs/architecture/job-engine.md` | DBOS guard rules |

### Symbol pins (machine-verified)

| Module | Symbol | Frozen value / note |
|---|---|---|
| `mai.agents.run` | `HANDLER_NAME` | `"agent.run"` — legacy handler identity (§10) |
| `mai.agents.run` | `RUN_PAYLOAD_VERSION` | `1` — legacy payload frozen for replay |
| `mai.agents.run` | `AGENT_INTAKE_EVENT_TYPE` | `"agent.intake_interpreted"` — legacy-only type (S5) |
| `mai.agents.run` | `STREAM_DELTA_MAX_CHARS` | `4096` |
| `mai.agents.run` | `STREAM_TOOL_DETAIL_MAX_CHARS` | `32768` |
| `mai.agents.delegate` | `HANDLER_NAME` | `"agent.delegate"` — the ONE delegation handler |
| `mai.agents.delegate` | `DELEGATION_PAYLOAD_VERSION` | `1` — v1 single-server payload frozen |
| `mai.agents.delegate` | `MAX_CHILDREN_PER_RUN` | `8` |
| `mai.agents.delegate` | `MAX_DELEGATION_DEPTH` | `4` |
| `mai.agents.delegate` | `ChildSpec` | legacy single-server child spec (S2) |
| `mai.agents.delegate` | `DelegationIntent` | delegation payload root |
| `mai.agents.delegate` | `ParentRunRow` | parent linkage row |
| `mai.agents.delegate` | `plan_children` | child planning seam |
| `mai.agents.delegate` | `DelegationExecutor` | the delegation executor (extended, not replaced) |
| `mai.agents.budgets` | `BUDGET_RESERVED_EVENT_TYPE` | `"agent.budget_reserved"` |
| `mai.agents.budgets` | `BUDGET_RELEASED_EVENT_TYPE` | `"agent.budget_released"` |
| `mai.agents.budgets` | `BUDGET_CHARGED_EVENT_TYPE` | `"agent.budget_charged"` |
| `mai.agents.budgets` | `MAX_ACTIVE_RUNS` | budget vocabulary bound |
| `mai.agents.budgets` | `MAX_TOTAL_STEPS` | budget vocabulary bound |
| `mai.agents.budgets` | `MAX_TOTAL_TOKENS` | budget vocabulary bound |
| `mai.agents.intake` | `arun_intake_turn` | legacy intake planner entry |
| `mai.agents.tools` | `CapabilityToolBridge` | model→dispatcher bridge |
| `mai.agents.tools` | `CapabilityToolBridge.dispatch_once` | the approval one-shot resume seam |
| `mai.jobs.engine` | `register_job_handler` | the ONE handler registration seam |
| `mai.jobs.engine` | `RUN_WORKFLOW_NAME` | `"run_workflow"` — the ONE trampoline |
| `mai.jobs.engine` | `APPLICATION_VERSION` | `"mai-1"` — recovery compatibility pin |
| `mai.jobs.recovery` | `RECOVERY_PARKED_EVENT_TYPE` | `"run.recovery_parked"` |
| `mai.api.jobs` | `SUBMITTED_EVENT_TYPE` | `"run.submitted"` |
| `mai.api.chat` | `register_chat` | chat surface registration |
| `mai.api.chat` | `CHAT_STEERED_EVENT_TYPE` | `"chat.steered"` |
| `mai.api.conversations` | `register_conversations` | legacy read surface registration |
| `mai.contracts.conversations` | `DEFAULT_PAGE_LIMIT` | `50` |
| `mai.contracts.conversations` | `MAX_PAGE_LIMIT` | `200` |
| `mai.contracts.conversations` | `TurnInterpretation` | legacy intake union (S1) |
| `mai.contracts.jobs` | `RunState` | frozen lifecycle vocabulary |
| `mai.contracts.jobs` | `RunState.WAITING_CHILDREN` | `waiting_children` lifecycle edge |
| `mai.contracts.jobs` | `ENGINE_STATE_MAP` | engine→domain state map (fail closed) |
| `mai.contracts.tasks` | `TaskCompletionVerdict` | evidence-bound completion receipt |
| `mai.capabilities.dispatch` | `CapabilityDispatcher` | the ONE authorization boundary |
| `mai.policy.config` | `GrantConfigStore` | versioned policy config store |
| `mai.policy.evaluate` | `Policy` | revision-bound policy |
| `mai.policy.evaluate` | `ParentScope` | parent binding of a delegated context |
| `mai.history_store` | `ConversationSearchRow` | legacy history read row |

## 14. Design targets (not yet implemented — producer slices freeze exact shapes)

<!-- design-targets:begin -->
Everything between these markers is a frozen design target, NOT an existing repo
symbol. Producers implement them as named; renames require reconciliation of
this document and its consumers.

- Handler: `agent.run_v2` (registered by `action-runtime` via
  `mai.jobs.engine.register_job_handler`; module layout
  `backend/src/mai/agents/run_v2.py` proposed, exact module path owned by the
  producer).
- Intent: v2 run payload with `payload_version` `2` (reconciled N1: matches
  `RUN_PAYLOAD_VERSION_V2 = 2` in `backend/src/mai/agents/run_v2.py`,
  beside the `ChildSpecV2` delegation payload version `2`); strict
  unknown-field rejection.
- Delegation: payload version `2` child specification `ChildSpecV2`
  (fields `server_ids`, `capabilities`, budget bounds) — emitted by v2 parents
  through the existing `agent.delegate` engine.
- `objective.*` event types: `objective.created`, `objective.scope_changed`,
  `objective.status_changed`, `objective.journal_updated`,
  `objective.checklist_updated`, `objective.completed` (exact set owned by
  `journal-store`; additions stay in-family and additive).
- Persistence: objective/journal/checklist tables + their Alembic revisions
  (ONE migration owner: `journal-store`).
- Routes: `/api/v1/objectives*` families per §11 (paginated; C01 envelope).
- Actor-kind field on activity events where provenance matters (§4).
<!-- design-targets:end -->

## 15. Change control

This document is frozen by the `contracts` task of the `runtime-v2` campaign.
Changes require campaign reconciliation (pause affected dispatch, record the
change, re-issue briefs) — silent edits are prohibited. The machine-verified
pins (§13) and the scenario registry rows (§12) must be re-frozen in the same
motion as any producer slice that changes a pinned name. Consumers cite section
IDs of this document, never draft prose.

## 16. Provenance

Assembled 2026-11-01 by the `contracts` task on branch
`megastruktur/rv-contracts` (base `07eb3b2179c0305d5dd778767eabccd18d424716`)
from the converged `rv_INTENT.md`/`rv_RESEARCH.md`/`rv_CRITIQUE.md` (hashes in
the header; verdict CONVERGED round 2). Documentation-only; executable
consistency checks live in `backend/tests/test_runtime_v2_contracts.py`;
evidence in `plans/runtime-v2/rv_evidence/contracts/`.
