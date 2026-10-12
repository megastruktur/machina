# Job engine — decision and operating rules (P00)

Status: accepted P00 output for card `mai-00-job-spike`. Consumers: P05 (`job-schema`,
`engine-adapter`, `run-lifecycle`, `steps`, `cancel-recovery`, `schedules`) and the
`jobs` module row in the service architecture. Input contracts: C04 (SSH execution and
remote outcomes), C05 (runs, steps, schedules and shared locks). Evidence:
`mai_evidence/00-job-spike/` (8 experiments, 2026-10-12, disposable Postgres 18 fixture,
probe source `experiments/durable_job_probe.py`).

## Decision

**DBOS (`dbos-transact-py` 3.2.0) is the durable engine for Machina jobs**, confirmed by
the interruption/child-run spike. Fallback (app-owned Postgres worker, outbox +
SKIP LOCKED) was NOT triggered: addendum pass criteria — experiments 1, 3, 4, 7 pass,
experiment 2 documents the expected double-effect window — are met.

Rejected alternatives and the reasoning trail live in the decision record
(`plans/mai-v1/DECISION_DJOBS.md`); Temporal was rejected on operational merit
(server cluster + own datastore + ES + sequential upgrade treadmill) — not re-litigated here.

## Tested version, dependencies, run-state ownership (acceptance 1)

- Tested exactly: `dbos==3.2.0` (dbos-transact-py, MIT), Python 3.14.6, Postgres 18.
- Declared dependencies: `click`, `psycopg`, `python-dateutil`, `pyyaml`, `sqlalchemy`,
  `websockets`. Library-only: no orchestration server, no broker, no extra service.
  Conductor/Console (vendor UI) is proprietary-licensed and NOT used; Machina's own
  API/UI is the management surface (`DBOS.list_workflows` class of APIs, verified).
- Run-state ownership: DBOS creates and owns a `dbos` schema inside Machina's existing
  Postgres (`workflow_status`, `workflow_input`, `workflow_output`, `operation_outputs`,
  `workflow_events`, `workflow_events_history`, `workflow_schedules`, `queues`,
  `notifications`, `streams`, `event_dispatch_kv`, `application_versions`,
  `dbos_migrations`). Per the SERVICES canonical-ownership row, the chosen job
  facade/engine map in Postgres owns runs, steps and events; DBOS tables are that map —
  no second job-state authority is introduced. Workflows' durable payloads live in the
  same Postgres Machina already backs up: `pg_dump`/restore was exercised directly (exp7).
- The engine owns: checkpoint/replay, workflow-step state, queues, schedules, child
  edges, recovery. Machina owns: domain run API, grants/approval gating, SSH effects,
  reconciliation of uncertain outcomes, and the event projection shown to the browser.

## Measured semantics (what the spike proved)

| Property | Measured behaviour (experiment) |
|---|---|
| Completed steps never re-execute | kill -9 mid-step: completed steps replayed from checkpoints, in-flight step re-executed once, downstream ran once (exp1) |
| Crash between effect and checkpoint | NOT closed by the engine: step re-executed after kill, effect ran twice, run still completed (exp2). No engine makes a host mutation exactly-once — identical gap in DBOS/Temporal/custom; the differentiator is the app-level guard (rules below) |
| Submit dedup | `SetWorkflowID` makes the workflow ID an idempotency key: concurrent submits resolve to one execution, all callers get the same handle; `workflow_id_reuse_policy="reject"` raises `DBOSWorkflowIDInUseError` (exp3) |
| Parent/child trees | Children via `DBOS.start_workflow` inside the parent workflow are checkpointed edges: parent kill -9 mid-await + restart re-attached, zero duplicate children, parent completed with all child results (exp4a) |
| Tree cancellation | Default `cancel_workflow(parent)` does NOT touch children — children ran to completion (exp4b). `cancel_children=True` cancels children at their next step boundary: in-flight non-preemptible steps ran to completion, then stopped (exp4c). Cancel is requested-not-verified at the effect boundary (C04) |
| Schedules | Cron schedules persist in the system DB; after 3 min downtime the missed fires are SKIPPED (no catch-up, no stampede) and firing resumes from "now", deduplicated per fire time by deterministic workflow ID (exp5). `automatic_backfill=True` exists but was not measured — off by default |
| Cancellation point | Mid-step cancel preempts at the NEXT step boundary; a step marked `preemptible=True` (async) cancels immediately (exp6). Awaited cancels surface as `DBOSAwaitedWorkflowCancelledError` |
| Backup/restore | pg_dump mid-queue (jobs in flight mid-step) → restore to a fresh database → relaunch: in-flight jobs resumed from their last checkpoint, queued jobs ran once, completed jobs stayed completed — every observable effect exactly once (exp7). Single-datastore backup acceptance holds |
| Postgres outage | Workflow blocked through a 60s outage (no step failure), completed ~8s after the database returned; manual `resume_workflow` remains the repair path for workflows that land in ERROR (exp8) |

## Responsibility split (C05)

- Application facade (`backend/src/mai/jobs/` proposal) owns the domain run API:
  submit (with idempotency key → workflow ID), cancel request, list/replay, schedule
  management. It never exposes engine internals to routers/UI.
- One engine adapter wraps DBOS; engine-specific vocabulary (workflow handles, queues,
  step infos) stops at the adapter boundary. No second scheduler and no hidden queue
  outside DBOS queues.
- Run lifecycle mapping (C05 proposal): `queued` = ENQUEUED, `running` = PENDING,
  `succeeded` = SUCCESS, `failed` = ERROR, `cancelled` = CANCELLED;
  `waiting_approval`/`waiting_children`/`reconciling` are application states layered on
  top (approval wait is an application-side step/signal pattern, not an engine state).
  `cancel_requested` is recorded by the facade until the engine reports CANCELLED —
  exp4c/exp6 show cancellation lands at step boundaries, so "requested" ≠ "verified".
- Child runs: delegation trees use checkpointed child edges (exp4a). Parent yields on
  child handles; nesting/budget caps remain application-level (C05), the engine does not
  enforce Machina budgets.
- Events: `workflow_events`/`workflow_events_history` back durable
  signals/streaming; the browser event projection and SSE replay remain a facade
  concern (C05 "stream is a view of persisted events").
- Workers: multiple app processes share one system DB; workflows are recovered on
  launch (measured via kill/restart). Out-of-process control MUST use a bare client
  (e.g. `DBOSClient`) — launching a second full app instance steals pending workflows
  via launch recovery (observed during probe development).

## P05 mandatory guard rules (spike-derived, binding for engine consumers)

1. **Verify-then-mutate is mandatory for host-mutating steps.** The effect-before-
   checkpoint window is real and measured (exp2). Every step that mutates a target host
   MUST be structured as [verify step → mutate step] or be idempotent by construction;
   the verify step reads host state first and the mutate step is skipped when the
   desired state already holds. A bare "run command" step is only allowed for
   read-only/recoverable effects.
2. **Missing receipt is `outcome_unknown`, never retry-safe.** If a step's effect may
   have landed but its checkpoint did not (kill, outage, cancel), the run records
   `outcome_unknown` for that effect and reconciliation (C04) decides; no automatic
   duplicate dispatch while a prior process may live.
3. **Cancellation is requested, not verified.** `cancel_workflow` preempts at step
   boundaries; in-flight non-preemptible steps run to completion and their effects
   happen (exp4c, exp6). Cancellation flows MUST re-verify target state (rule 1) and
   the UI/API must not report "cancelled" before the engine status says CANCELLED.
   Use `preemptible=True` async steps only for effects that are safe to interrupt.
4. **Tree cancellation is explicit.** Children are independent durable workflows;
   cancelling a parent leaves them running unless `cancel_children=True` is passed
   (exp4b/4c). The facade's cancel-delegation-tree operation must decide propagation
   explicitly and account for descendants (C04 cancellation rule).
5. **Submit dedup via deterministic workflow IDs; conflicting resubmits rejected.**
   Domain run IDs are the workflow IDs (C01 opaque UUIDs); duplicate submits with the
   same ID dedup to one run (exp3). API paths that must not attach to an existing run
   (e.g. a genuinely new run that lost an ID race) use `workflow_id_reuse_policy="reject"`
   and surface `DBOSWorkflowIDInUseError` as a structured conflict, not a 500.
6. **Schedules skip missed fires by default.** Misfire policy is "resume from now, no
   catch-up" (exp5). If a collector must not skip windows (e.g. monitoring scans), the
   scheduled workflow itself must compute its window from the passed scheduled time —
   or `automatic_backfill` must be deliberately evaluated before use (unmeasured).
7. **Schedule fire times are workflow arguments.** Time-of-day logic belongs inside the
   scheduled workflow (which receives its scheduled fire time), never in the cron
   expression beyond the recurrence; timezone is an explicit `cron_timezone` (default UTC).
8. **Recovery:** relaunching a worker recovers pending workflows (exp1, exp4a, exp7);
   ERROR workflows stay ERROR until explicitly resumed. The facade must expose
   resume/fork as deliberate operations (C05), never automatic blind replay.
9. **Backup/restore = single pg_dump.** Engine state rides in Machina's database;
   restore to a fresh database and relaunch is a supported recovery path (exp7). No
   separate engine datastore may be introduced without reopening this decision.
10. **Postgres outage pauses work; it does not corrupt it.** Workflows block and
    complete after recovery (exp8); steps whose effect landed before a lost checkpoint
    fall under rule 2.

## Fallback conditions (not triggered)

The app-owned worker becomes the engine only if a future DBOS upgrade breaks measured
semantics above (checkpoint-replay, child edges, restore recovery) — the spike probe is
re-runnable as a regression gate (`experiments/durable_job_probe.py`, per-experiment
verdicts). DBOS state being plain Postgres tables keeps the exit a data migration.
