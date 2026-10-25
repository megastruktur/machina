/**
 * Feature-owned task service boundary — card `mai-16-task-client` (§8 task
 * API/UI contract, producer `15-task-api`).
 *
 * This module is the ONE panel-facing vocabulary of the task surface. It is
 * a SERVICE boundary, not a panel registration: no task panel component
 * exists yet (no `panels/TaskPanel.svelte`, no `panel_kind` in the
 * registry), so there is no `PanelEntryInput` here — the entry's job is to
 * keep the typed wire shapes and the typed services bag single-sourced for
 * the §8 task area that will consume them (the compact inline task area of
 * the conversation surface, per §8, not a separate global application).
 *
 * It owns:
 *
 *  - the typed wire shapes the task UI consumes, re-exported from the ONE
 *    reviewed client transport (`../../api/client.ts`, every one of them
 *    derived verbatim from `schema.generated.ts` — no invented endpoints,
 *    C08 rule 8, no hand-restated fields);
 *  - the typed services bag the §8 task area requires, mirroring the
 *    reviewed producer routes 1:1 (`backend/src/mai/api/tasks.py`): the
 *    compact header read, the four bounded collection pages and the two
 *    CAS + idempotency-guarded mutations.
 *
 * Authority honesty (§6/§8): plan review (revisions), decisions (phase/
 * pause/cancel) and operation approvals (the C03 `/api/v1/approvals`
 * surface) are three distinct routes with distinct effects; this bag never
 * merges them. Phase selection is advisory, never permission; a metadata
 * decision cannot change grants.
 *
 * No module-level mutable state; safe to evaluate per SSR request (ADR §5
 * rule 4). Composition passes `taskServices({ …clientTaskMethods })` — the
 * composed client grows the task transport methods; instances still share
 * nothing, and nothing here ever touches browser storage (the server is the
 * only source of task truth, P5).
 */

import type {
	TaskCollectionQuery,
	TaskConstraintView,
	TaskConstraintsPage,
	TaskCorrectionBody,
	TaskDecisionBody,
	TaskEvidencePage,
	TaskMutationOutcome,
	TaskRevisionsPage,
	TaskStepView,
	TaskStepsPage,
	TaskView
} from '../../api/client.js';

/**
 * Task wire shapes (§8; 15-task-api) — the exact generated-schema types the
 * client transports, re-exported here so the panel-facing service vocabulary
 * stays single-sourced.
 */
export type {
	ConstraintProposal,
	EvidenceRef,
	TaskCollectionQuery,
	TaskConstraintView,
	TaskCounts,
	TaskCorrectionBody,
	TaskDecisionBody,
	TaskEvidencePage,
	TaskEvidenceView,
	TaskMutationOutcome,
	TaskOutcome,
	TaskPlanPatch,
	TaskPlanStep,
	TaskRevisionsPage,
	TaskRevisionView,
	TaskStepOperation,
	TaskStepView,
	TaskStepsPage,
	TaskView
} from '../../api/client.js';

/** Optional bounded-page query of the offset-paged task collections (C01 rule 5). */
export interface TaskPageQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

/**
 * The task API slice the §8 task area consumes, injected via the panel
 * services bag (satisfied by the composed client once it grows the
 * 15-task-api transport methods). Mirrors the reviewed routes 1:1; reads
 * are session-checked server-side, mutations additionally origin+CSRF-
 * checked (auth ADR §5) — the bag never weakens that. Every method takes
 * its explicit `server_id` (C01 rule 2); every collection is a bounded page
 * (AGENTS.md §1).
 */
export interface TaskApi {
	/**
	 * GET /api/v1/chat/conversations/{id}/task?server_id=… — the
	 * conversation's ONE active task header; absent conversation, foreign
	 * scope and absent episode collapse into the SAME scope-blind 404.
	 */
	getConversationTask(conversationId: string, serverId: string): Promise<TaskView>;
	/**
	 * GET /api/v1/chat/tasks/{id}/items?server_id=… — deterministic
	 * `(position, step_id)` plan-step page; superseded rows stay recorded
	 * (P3) and are served only when `includeSuperseded` asks for them.
	 */
	listTaskSteps(
		taskId: string,
		serverId: string,
		query?: TaskCollectionQuery
	): Promise<TaskStepsPage>;
	/**
	 * GET /api/v1/chat/tasks/{id}/constraints?server_id=… — deterministic
	 * `(position, id)` constraint page carrying origin and source-message
	 * anchors.
	 */
	listTaskConstraints(
		taskId: string,
		serverId: string,
		query?: TaskCollectionQuery
	): Promise<TaskConstraintsPage>;
	/**
	 * GET /api/v1/chat/tasks/{id}/evidence?server_id=… — provenance
	 * identities only; referenced content is never inlined.
	 */
	listTaskEvidence(
		taskId: string,
		serverId: string,
		page?: TaskPageQuery
	): Promise<TaskEvidencePage>;
	/**
	 * GET /api/v1/chat/tasks/{id}/revisions?server_id=… — the append-only
	 * shared revision ledger, newest first (the P5 reconnect truth).
	 */
	listTaskRevisions(
		taskId: string,
		serverId: string,
		page?: TaskPageQuery
	): Promise<TaskRevisionsPage>;
	/**
	 * POST /api/v1/chat/tasks/{id}/revisions — one authenticated user
	 * correction/proposal under CAS + idempotency (201 created / 200
	 * deduplicated / 409 revision_conflict / scope-blind 404
	 * task_not_found). Always author="user", always source-linked.
	 */
	submitTaskCorrection(
		taskId: string,
		payload: TaskCorrectionBody
	): Promise<TaskMutationOutcome>;
	/**
	 * POST /api/v1/chat/tasks/{id}/decisions — one explicit user decision:
	 * advisory `requested_phase` or pause/cancel through the frozen outcome
	 * transitions. Never applies a plan delta, never authorizes or
	 * terminates a pending operation (that is the C03 approvals surface).
	 */
	submitTaskDecision(taskId: string, payload: TaskDecisionBody): Promise<TaskMutationOutcome>;
}

/**
 * Typed services bag the §8 task area requires from its opener. The `api`
 * slice is REQUIRED (a missing/incomplete bag fails closed — a host without
 * the 15-task-api client methods renders no task truth rather than a stale
 * local copy).
 */
export interface TaskServices {
	readonly api: TaskApi;
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `taskServices({ …clientTaskMethods })` into the panel host). Freezing
 * keeps the bag read-only from the panel's side; the panel still fails
 * closed if the bag is missing or incomplete.
 */
export function taskServices(api: TaskApi): TaskServices {
	return Object.freeze({ api });
}

// ---------------------------------------------------------------------------
// §8 understanding block — read-only service wiring (act `17-understanding-ui`)
// ---------------------------------------------------------------------------

/**
 * Bounded page size the §8 understanding block uses for EVERY section
 * (AGENTS.md §1): the header read carries counts only, and the constraint
 * and plan-step sections are fetched as explicit bounded pages — never an
 * unbounded load-whole-task dump.
 */
export const TASK_UNDERSTANDING_PAGE_LIMIT = 10;

/**
 * One bounded understanding fetch through the task api slice (§8): the
 * compact header plus the FIRST bounded page of the active constraint and
 * plan-step collections. Superseded rows stay recorded server-side (P3)
 * and are NOT fetched here — the understanding block presents the live
 * plan, and history is reachable through the same api by name.
 */
export interface TaskUnderstandingSections {
	/** The compact §8 header view (revision, phase, status, counts). */
	readonly header: TaskView;
	/** First bounded page of active constraints, deterministic (position, id) order. */
	readonly constraints: readonly TaskConstraintView[];
	/** Pre-slice active-constraint count under the identical filter. */
	readonly constraintsTotal: number;
	/** True when another constraint page exists after this slice. */
	readonly constraintsHasMore: boolean;
	/** First bounded page of live plan steps, deterministic (position, step_id) order. */
	readonly steps: readonly TaskStepView[];
	/** Pre-slice live step count under the identical filter. */
	readonly stepsTotal: number;
	/** True when another step page exists after this slice. */
	readonly stepsHasMore: boolean;
}

/**
 * Read-only understanding fetch (act `17-understanding-ui`): one header read
 * plus the two first section pages, all through the ONE injected api slice.
 * No mutation is ever issued; transport failures (including the scope-blind
 * 404 `task_not_found` of an absent episode) propagate as `ApiError` so the
 * consuming block classifies them honestly instead of fabricating content.
 */
export async function loadTaskUnderstandingSections(
	api: TaskApi,
	conversationId: string,
	serverId: string
): Promise<TaskUnderstandingSections> {
	const header = await api.getConversationTask(conversationId, serverId);
	const taskId = header.task_id;
	const [constraints, steps] = await Promise.all([
		api.listTaskConstraints(taskId, serverId, {
			limit: TASK_UNDERSTANDING_PAGE_LIMIT,
			offset: 0
		}),
		api.listTaskSteps(taskId, serverId, { limit: TASK_UNDERSTANDING_PAGE_LIMIT, offset: 0 })
	]);
	return {
		header,
		constraints: constraints.items ?? [],
		constraintsTotal: constraints.total,
		constraintsHasMore: constraints.has_more,
		steps: steps.items ?? [],
		stepsTotal: steps.total,
		stepsHasMore: steps.has_more
	};
}
