# MAI acceptance — assembled end-to-end journeys (P12)

- **Card:** `12-end-to-end` (P12 · service `testing` · risk `high`)
- **Producer test:** `tests/runtime/test_journeys.py` (15 focused journeys,
  one module session, disposable fixtures under the `mai-fxt-12-e2e`
  namespace with complete teardown — runtime-environment ADR D1–D5).
- **Evidence:** `mai_evidence/12-end-to-end/` (campaign evidence root,
  outside disposable worktrees — C10 rule 1).
- **Scope:** this document records how the SERVICES §"End-to-end execution
  route" and the idea §16 outcomes are reproduced by the owned test; it is
  not an operations runbook (see `recovery.md`, `uninstall.md`).

## Recorded component versions

Journey captures were produced against the versions recorded in
`mai_evidence/12-end-to-end/01-commands.txt` (toolchain section): engine
`dbos==3.2.0`; fixture cluster image `pgvector/pgvector:0.8.1-pg18` (real
Alembic chain + engine-owned `dbos` schema + the `hindsight` bank
database); disposable Arch target built from `archlinux:latest` +
`openssh` (same proven recipe as P04/P10); model stack
`pydantic-ai-slim[openai] 2.56.x` over `openai 3.x`; `ruff` 0.15.6 for the
static gate. The stub endpoints (embeddings, chat completions) are
loopback-only stdlib servers disclosed below.

## The route under test

```
browser/API intent (real ASGI app + auth boundary)
  → enrollment (canonical profile, pinned host key)
  → policy/grant/approval gates (real CapabilityDispatcher)
  → verified SSH execution with receipts (real supervisor lane)
  → verified/failed/unknown outcome + explicit reconciliation
  → monitoring/security collection → findings close → notification dedup
  → knowledge storage/index at the pgvector boundary (server-scoped)
  → hybrid retrieval + deep-research investigation, citation-verified
    synthesis at the reviewed model-facade boundary (disclosed stub)
  → memory provider failure honesty; local-model target facts
  → backup/verify/restore round trip (auth+profile+banks survive)
  → uninstall plan discipline (explicit data intent, value-free)
  → scheduled delegation: headless schedule fire (real worker engine)
    → durable child run (progress + events exposed) → outcome retained
    → mid-run worker interruption: crash window → outcome_unknown →
      reconciling park → explicit host-verified resolution, never a
      blind repeat of the executed effect
```

## Journey → idea §16 outcome map

| Idea §16 outcome | Test (`tests/runtime/test_journeys.py`) | Real boundary exercised (and honest limits) |
|---|---|---|
| **A — install/maintain an application** | `TestJourneyApplicationLifecycle::test_plan_adopt_list_and_revoke_with_revision_discipline` + the approved-execution leg of `TestJourneyEndToEndRoute` | real catalogue recipe plan preview (pure, concrete resources), explicit adoption (canonical manifest + projection + record doc), inventory, duplicate/stale-revision conflicts, tombstone-first non-destructive revoke; execution half: dispatcher → payload-bound approval → real host-shell capability → real sshd fixture. Target-side deploy effects stay the `08-*` producer gates. |
| **B — investigate** | `TestJourneyInvestigateResearch::test_investigate_hybrid_search_deep_research_citations_and_incident` | real `mai.knowledge.retrieve query` production entrypoint: hybrid pgvector+FTS with RRF over real 1536-dim rows, full citation tuple per excerpt, foreign-scope invisibility (C07 rule 2); real `ResearchWorkflow` over the ONE dispatcher (server-pinned context, scoped docs); citation-verified synthesis at the reviewed model-facade boundary — `ModelConfigStore.bind` → `ModelBinding.rebuild` (call-time `.env` credential) → PydanticAI over the OpenAI SDK — against a disclosed loopback protocol stub; fabricated claim bases dropped (`dropped_claims`), kept claims cite recorded sources; canonical incident promotion on a failed run (C07 rule 5) and its retrieval after the real reconcile producer. Limits: the live model-gateway lane is KB-E-LLM's surface; `web.search`/`web.read`/`host.logs` are not wired into this composition (unrequested payloads — nothing dispatches them); memory write-back stays `11-*`. |
| **C — explain an existing server** | `TestJourneyBootstrapAuthEnrollment::test_bootstrap_login_enroll_and_negative_controls` + `TestJourneyMonitoringSecurityNotifications::test_collect_evaluate_fix_and_dedup_on_real_target` + `…::test_local_model_facts_measured_over_real_ssh_lane` | server identity pinned by the real `mai.cli bootstrap` + profile projection read back non-stale with its pinned key; measured server facts over the real SSH lane (security collector observations/findings, honest open listener; local-model facts with fail-closed unsatisfied tokens — discovered facts stay distinct from unknowns). Discovery never adopts anything for maintenance (adoption is A's explicit decision). |
| **D — scheduled delegated work** | `TestJourneyScheduledDelegation::test_scheduled_fire_executes_child_with_progress_and_retained_outcome` and `…::test_fault_worker_interruption_mid_effect_never_repeats_it` | headless with NO browser and NO HTTP call: real `ScheduleManager` trigger → real `schedule_fire_workflow` in a real `python -m mai.jobs.worker` process → `dispatch_fire` ledger + deterministic run identity → durable child run executing real `host.shell` on the fixture target through a bound approval; progress exposed (`job_runs` transitions, `job_steps` + `step.started`/`step.completed` events); outcome retained after the worker process is gone; interruption fault: SIGKILL mid-effect → recovered attempt translates the crash window to `outcome_unknown` (never re-dispatch), run parks `reconciling`, and only an explicit host-verified `RecoveryService.aresolve_step` verdict lands it `succeeded` — the effect executed EXACTLY once. Limits: the worker-side step composition rides the sanctioned `configure_steps` seam (production compose wiring is the platform card's surface); `agent.delegate` child trees are `06-child-runs`' surface. |

## Leg → test index

| Route leg | Test (`tests/runtime/test_journeys.py`) |
|---|---|
| C: enroll/auth negatives | `TestJourneyBootstrapAuthEnrollment::test_bootstrap_login_enroll_and_negative_controls` |
| fault: DB failure | `…::test_fault_database_outage_reports_structured_readiness` |
| A/D: approved execution | `TestJourneyEndToEndRoute::test_approved_command_executes_with_receipt_then_approval_spent` |
| fault: wrong server / unadopted | `…::test_fault_wrong_server_and_unadopted_denied_before_effect` |
| fault: host reboot | `…::test_fault_host_key_rotation_refused_then_explicit_reenrollment` |
| §7 monitoring/notifications | `TestJourneyMonitoringSecurityNotifications::test_collect_evaluate_fix_and_dedup_on_real_target` |
| §11 memory / KB-H2 failure half | `TestJourneyMemoryAndLocalModels::test_memory_bank_validation_and_failure_is_never_success` |
| §8 local models | `…::test_local_model_facts_measured_over_real_ssh_lane` |
| §10 knowledge / C07 | `TestJourneyKnowledgeBoundary::test_docs_chunks_and_server_scope_on_real_pgvector` |
| §16 B investigate | `TestJourneyInvestigateResearch::test_investigate_hybrid_search_deep_research_citations_and_incident` |
| §6 applications (C09) | `TestJourneyApplicationLifecycle::test_plan_adopt_list_and_revoke_with_revision_discipline` |
| BACKUP-RESTORE | `TestJourneyBackupRestoreUninstall::test_backup_verify_restore_preserves_auth_profile_and_profile_state` |
| C09 rule 5 / uninstall | `…::test_uninstall_plan_is_read_only_and_value_free` |
| §16 D scheduled delegation | `TestJourneyScheduledDelegation::test_scheduled_fire_executes_child_with_progress_and_retained_outcome` |
| §16 D / fault: worker interruption | `…::test_fault_worker_interruption_mid_effect_never_repeats_it` |

## Fault matrix (acceptance 2)

| Fault | Where covered | Outcome |
|---|---|---|
| Disconnected browser | out of this writer's lane — WS rows are P01-owned; the API event/replay halves are owned by `05-jobs-api`/`05-web-jobs` (recorded in the card README limits). The scheduled-delegation legs prove the worker route runs with no browser and no HTTP surface at all. | n/a here (D legs: headless proven) |
| API/worker/DB failure | API/DB: `test_fault_database_outage_reports_structured_readiness` (liveness truthful, readiness 503, no URL/secret material in any body). Worker: `test_fault_worker_interruption_mid_effect_never_repeats_it` — SIGKILL mid-effect, launch recovery replays the workflow, the crash window becomes `outcome_unknown` (`reason=crash_recovery`), the run parks `reconciling`, the executed effect is NEVER re-dispatched, and an explicit host-verified verdict resolves the run `succeeded` with the outcome retained in the engine + facade rows. DB under the worker: the whole module runs against the real fixture cluster; the restore leg proves the jobs/engine rows ride the backup boundary. | honest, no blind repeat, retained |
| SSH loss / unproven effect | rotation refusal leg of `test_fault_host_key_rotation_refused_then_explicit_reenrollment` (effect `unknown`, never `executed`); the interruption fault leg (`effect=unknown` crash receipt, reconciling park); transport-loss statuses are the P04 gate's measured evidence (exp3) | unproven, honest |
| Revoked grant / wrong server | `test_fault_wrong_server_and_unadopted_denied_before_effect` + approval-binding denial | denied before effect |
| Host reboot (host key changed) | `test_fault_host_key_rotation_refused_then_explicit_reenrollment` | refuse → explicit re-enrollment → allowed |
| Stateful update/removal | `TestJourneyApplicationLifecycle` (stale-revision conflicts, tombstone-first revoke, non-destructive guarantee) + approval single-use + C09 explicit `--keep-data` plan intent; full update/remove target journeys remain the `08-*` producer gates | covered at the intent boundary |

## How to run

```bash
cd backend && uv sync --frozen
.venv/bin/python -m pytest ../tests/runtime/test_journeys.py -v
```

Requires the Docker CLI and the disposable-fixture namespace
`mai-fxt-12-e2e` (containers `mai-fxt-12-e2e-pg[-restore]`,
`mai-fxt-12-e2e-ssh`, `/tmp/mai-fxt-12-e2e`). Teardown removes every
resource and prints the cleanup proof; a stray leftover fails the run.

## Limits (disclosed)

- The durable worker is exercised through its sanctioned seams: the
  scheduled-delegation legs launch the REAL `python -m mai.jobs.worker`
  entrypoint, and the worker-side step composition rides the documented
  `configure_steps` seam (the production compose wiring is the platform
  card's surface). No second scheduler is started.
- The embeddings endpoint is a protocol stub (deterministic 1536-dim;
  KB-E1 owns the real endpoint); the investigate leg's synthesis model is
  exercised at the REAL reviewed model-facade boundary
  (`ModelConfigStore.bind` → `ModelBinding.rebuild` → PydanticAI/OpenAI
  SDK) against a disclosed loopback `/v1/chat/completions` protocol stub
  (the live-model gateway lane is KB-E-LLM's surface).
- The Hindsight service compose boundary is `11-hindsight-compose`'s
  measured evidence — here the provider's real network path is exercised
  against an unreachable endpoint (failure honesty).
- Browser window journeys are the assembled `12-verify` gate's surface;
  the scheduled-delegation legs prove the headless (no browser) half of
  Outcome D.
- The knowledge retrieval assertions run at the storage/retriever boundary
  (server separation + dimension contract + RRF excerpts with full
  citation tuples); the `web.search`/`web.read`/`host.logs` capabilities
  are not wired into the investigate composition — their requests are
  simply not made, so nothing dispatches them (the search lane is
  `07-searxng`'s surface, the ranking gate `07-retrieval`'s).
