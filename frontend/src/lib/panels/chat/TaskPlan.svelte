<script lang="ts">
	/**
	 * TaskPlan — compact §8 Plan section (act `18-plan-ui`; act_CONTRACTS
	 * §7/§8): the ONE active task episode's persistent todos, dependencies,
	 * progress and evidence links, plus the user REVISION-PROPOSAL surface.
	 *
	 * Data comes exclusively through the injected `07-web-tasks.ts` service
	 * bag; the block owns no browser state (the server is the only task
	 * truth, P5) and no module-level singletons (ADR §5 rule 4).
	 *
	 * Revision honesty (criterion 1; §1/§8):
	 * - A user edit NEVER rewrites task history in place. Every edit is one
	 *   bounded `TaskPlanPatch` proposal sent through `submitTaskCorrection`
	 *   under compare-and-swap (`base_revision`) + idempotency. "Remove" is
	 *   a supersede: the replaced row stays recorded server-side (P3).
	 * - A stale `base_revision` surfaces the structured 409
	 *   `revision_conflict` as an explicit conflict state — nothing is
	 *   silently overwritten and no fake success is shown.
	 * - The proposal anchors to a recorded user message of the conversation
	 *   (`source_message_id`, §8 "always source-linked"); without one the
	 *   form renders its explicit disabled state instead of inventing an
	 *   anchor.
	 * - Dispositions are honest: `created` (applied as revision N),
	 *   `deduplicated` (already recorded), pending while in flight.
	 *
	 * Running-item honesty (criterion 2): removing an `in_progress` item is
	 * labelled a proposal that does NOT stop any running process — the run
	 * reports its real state; only the executor acknowledges at a safe
	 * boundary (§8 "metadata removal of a step does not stop an
	 * already-running command").
	 *
	 * Executed vs verified are distinct axes (§7) and render as two
	 * visually distinct, always-text-labelled badges — never one shared
	 * "done" badge.
	 *
	 * Bounded everywhere (AGENTS.md §1): fixed pages inside
	 * `overflow-y: auto` containers, "Showing n of m" lines, "Load more"
	 * controls, appends deduplicated by stable id (across AND within one
	 * incoming page). A bounded poll re-reads the header so task-tool
	 * updates made while disconnected become visible on reconnect; pages
	 * reload only when the server revision actually moved.
	 *
	 * Svelte 5 runes only; tokens only; no `window`/`document`/storage.
	 */
	import Button from '../../ui/Button.svelte';
	import Field from '../../ui/Field.svelte';
	import Select from '../../ui/Select.svelte';
	import { ApiError } from '../../api/client.js';
	import {
		taskServices,
		type TaskApi,
		type TaskEvidenceView,
		type TaskRevisionView,
		type TaskServices,
		type TaskStepOperation,
		type TaskStepView,
		type TaskView
	} from '../../workspace/entries/07-web-tasks.js';

	/**
	 * The honest render uiState. `unbound` = no usable task api slice bound
	 * (nothing fetched, nothing fabricated); `unavailable` = the scope-blind
	 * 404 — no active task episode for this conversation in this scope;
	 * `error` = any other transport failure; `ready` = server-backed data.
	 * (Declared before any rune-initialized statement: svelte2tsx misorders
	 * type aliases interleaved after them.)
	 */
	export type PlanState = 'unbound' | 'loading' | 'unavailable' | 'error' | 'ready';

	/** The honest proposal lifecycle (pending/applied distinction, P5). */
	type ProposalState = 'idle' | 'pending' | 'applied' | 'conflict' | 'error';

	type ProposalOp = 'add' | 'update' | 'supersede';

	interface Props {
		/** The task service bag from the opener; a missing bag fails closed. */
		services: TaskServices | null;
		/** Conversation whose ONE active task episode is presented. */
		conversationId: string | null;
		/** Explicit server scope (C01 rule 2). */
		serverId: string | null;
		/**
		 * A recorded user message of this conversation that proposals anchor
		 * to (§8: always source-linked); `null` honestly disables the form.
		 */
		sourceMessageId?: string | null;
		/** Bounded page size override (tests); server bounds still apply. */
		pageSize?: number;
		/** Bounded header-poll interval in ms; 0 disables the poll (tests). */
		pollMs?: number;
		/**
		 * Optional parent binding (cti-01 Task Inspector drawer): mirrors
		 * `uiState` upward so the host trigger/badge reads the honest render
		 * state without any second network read. Absent binding ⇒ local only.
		 */
		status?: PlanState;
		/** Optional parent binding: mirrors the ready task header (badge counts). */
		taskHeader?: TaskView | null;
		/**
		 * A4 recovery: read-only mode NEVER offers the recovery action.
		 */
		readOnly?: boolean;
		/**
		 * A4 legacy recovery: ONE canonical follow-up submission for an
		 * open task stranded in `inspect`. The parent performs the
		 * EXISTING composer submit (`requested_phase: 'act'`,
		 * `task_action: 'continue'`, this explicit `expectedRevision`, the
		 * pinned `server_id`) — never a decisions endpoint or a second
		 * executor. The promise resolves when the turn is durably admitted
		 * and rejects with the structured failure (stale revision,
		 * transport) so the block stays honest; it never claims task
		 * success. Absent callback ⇒ the action is never offered.
		 */
		onContinueWithChanges?: (expectedRevision: number) => Promise<void>;
	}

	let {
		services,
		conversationId,
		serverId,
		sourceMessageId = null,
		pageSize = 10,
		pollMs = 15000,
		status = $bindable<PlanState>('unbound'),
		taskHeader = $bindable<TaskView | null>(null),
		readOnly = false,
		onContinueWithChanges = undefined
	}: Props = $props();

	/** Bounded section page size (AGENTS.md §1). */
	const PAGE_LIMIT = $derived(pageSize > 0 ? pageSize : 10);
	/** Non-positive poll overrides collapse to "disabled". */
	const POLL_INTERVAL = $derived(pollMs > 0 ? pollMs : 0);

	let uiState = $state<PlanState>('unbound');
	let problem = $state<{ status: number; code: string; message: string } | null>(null);
	let header = $state<TaskView | null>(null);

	// cti-01: one-way mirror onto the optional parent bindings — the Task
	// Inspector drawer reads the honest state without duplicating a single
	// fetch (C08 rule 6). The effect only writes; it never reads the bindings.
	$effect(() => {
		status = uiState;
		taskHeader = header;
	});

	/** Deduplicated, append-only section pages (AGENTS.md §1). */
	let steps = $state<readonly TaskStepView[]>([]);
	let stepsTotal = $state(0);
	let stepsHasMore = $state(false);
	let loadingMoreSteps = $state(false);
	let evidence = $state<readonly TaskEvidenceView[]>([]);
	let evidenceTotal = $state(0);
	let evidenceHasMore = $state(false);
	let loadingMoreEvidence = $state(false);
	let revisions = $state<readonly TaskRevisionView[]>([]);
	let revisionsTotal = $state(0);
	let revisionsHasMore = $state(false);
	let loadingMoreRevisions = $state(false);

	// --- proposal form state --------------------------------------------------

	let proposalOp = $state<ProposalOp>('add');
	let targetStepId = $state('');
	let proposalTitle = $state('');
	let proposalRationale = $state('');
	let proposalState = $state<ProposalState>('idle');
	let proposalOutcome = $state<{ revision: number; disposition: string } | null>(null);
	let proposalProblem = $state<{ status: number; code: string; message: string } | null>(null);

	/** Stale-response guard: only the newest load may write uiState (C08 rule 6). */
	let epoch = 0;
	/** Set once the mount cycle ended; async continuations check it (ADR §7.2). */
	let disposed = true;
	/** Serializes header syncs so the poll never interleaves with a reload. */
	let syncing = $state(false);

	/** The api slice this block consumes; an incomplete bag fails closed. */
	function usableApi(bag: TaskServices | null): TaskApi | null {
		if (bag === null) return null;
		const api: TaskApi | undefined = bag.api;
		if (api === null || typeof api !== 'object') return null;
		if (
			typeof api.getConversationTask !== 'function' ||
			typeof api.listTaskSteps !== 'function' ||
			typeof api.listTaskEvidence !== 'function' ||
			typeof api.listTaskRevisions !== 'function' ||
			typeof api.submitTaskCorrection !== 'function'
		) {
			return null;
		}
		return api;
	}

	/** The service bag restated through the frozen factory (fail-closed). */
	const boundServices = $derived.by<TaskServices | null>(() => {
		const api = usableApi(services);
		return api === null ? null : taskServices(api);
	});

	function resetSections(): void {
		header = null;
		steps = [];
		stepsTotal = 0;
		stepsHasMore = false;
		evidence = [];
		evidenceTotal = 0;
		evidenceHasMore = false;
		revisions = [];
		revisionsTotal = 0;
		revisionsHasMore = false;
	}

	async function load(): Promise<void> {
		const current = boundServices;
		if (
			current === null ||
			conversationId === null ||
			conversationId === '' ||
			serverId === null ||
			serverId === ''
		) {
			uiState = 'unbound';
			problem = null;
			resetSections();
			return;
		}
		const currentEpoch = ++epoch;
		uiState = 'loading';
		problem = null;
		try {
			const api = usableApi(current);
			if (api === null) {
				uiState = 'unbound';
				return;
			}
			const next = await api.getConversationTask(conversationId, serverId);
			if (disposed || currentEpoch !== epoch) return;
			header = next;
			await reloadPages(next);
			if (disposed || currentEpoch !== epoch) return;
			uiState = 'ready';
		} catch (error) {
			if (disposed || currentEpoch !== epoch) return;
			if (error instanceof ApiError && error.status === 404) {
				uiState = 'unavailable';
			} else {
				uiState = 'error';
				problem = {
					status: error instanceof ApiError ? error.status : 0,
					code: error instanceof ApiError ? error.code : 'unavailable',
					message: error instanceof Error && error.message !== '' ? error.message : 'request failed'
				};
			}
		}
	}

	/**
	 * One bounded live/reconnect tick. With an episode bound: re-read the
	 * compact header and reload the pages ONLY when the server revision
	 * moved (P5: the UI and the tools share the server revision — no
	 * speculative refetch storm, no duplicated rows). Without one (the
	 * scope-blind 404): a quiet probe re-checks for an episode created
	 * later — still absent keeps the explicit `unavailable` label without
	 * a loading flash; an appearing episode promotes to a full load.
	 */
	async function syncTick(): Promise<void> {
		const api = usableApi(boundServices);
		if (api === null || syncing || disposed) return;
		if (header === null) {
			if (uiState !== 'unavailable' || conversationId === null || serverId === null) return;
			syncing = true;
			try {
				const next = await api.getConversationTask(conversationId, serverId);
				if (disposed) return;
				header = next;
				await reloadPages(next);
				if (disposed) return;
				uiState = 'ready';
			} catch {
				// Still absent (or briefly unreachable): the explicit label stays.
			} finally {
				syncing = false;
			}
			return;
		}
		const current = header;
		syncing = true;
		try {
			const next = await api.getConversationTask(current.conversation_id, current.server_id);
			if (disposed) return;
			const moved = next.revision !== current.revision;
			header = next;
			if (moved) await reloadPages(next);
		} catch {
			// Honest stop: the rendered state stays; the next tick retries.
		} finally {
			syncing = false;
		}
	}

	function appendUnique<T>(current: readonly T[], incoming: readonly T[], id: (item: T) => string): T[] {
		const seen = new Set(current.map(id));
		const appended: T[] = [];
		for (const item of incoming) {
			const key = id(item);
			if (seen.has(key)) continue;
			seen.add(key);
			appended.push(item);
		}
		return [...current, ...appended];
	}

	/**
	 * Re-read all three bounded collections for `task`. On a revision move
	 * (poll/reconnect/mutation) the already-loaded window is preserved:
	 * pages re-fetch `max(PAGE_LIMIT, loaded)` rows and REPLACE — appended
	 * history is never silently dropped, and stable ids keep row identity.
	 */
	async function reloadPages(task: TaskView): Promise<void> {
		const api = usableApi(boundServices);
		if (api === null) return;
		const stepLimit = Math.max(PAGE_LIMIT, steps.length);
		const evidenceLimit = Math.max(PAGE_LIMIT, evidence.length);
		const revisionLimit = Math.max(PAGE_LIMIT, revisions.length);
		const [stepPage, evidencePage, revisionPage] = await Promise.all([
			api.listTaskSteps(task.task_id, serverId ?? task.server_id, {
				limit: stepLimit,
				offset: 0
			}),
			api.listTaskEvidence(task.task_id, serverId ?? task.server_id, {
				limit: evidenceLimit,
				offset: 0
			}),
			api.listTaskRevisions(task.task_id, serverId ?? task.server_id, {
				limit: revisionLimit,
				offset: 0
			})
		]);
		steps = appendUnique([], stepPage.items ?? [], (item) => item.step_id);
		stepsTotal = stepPage.total;
		stepsHasMore = stepPage.has_more;
		evidence = appendUnique([], evidencePage.items ?? [], (item) => item.evidence_id);
		evidenceTotal = evidencePage.total;
		evidenceHasMore = evidencePage.has_more;
		revisions = appendUnique([], revisionPage.items ?? [], (item) => String(item.revision));
		revisionsTotal = revisionPage.total;
		revisionsHasMore = revisionPage.has_more;
		if (targetStepId !== '' && !steps.some((step) => step.step_id === targetStepId)) {
			targetStepId = '';
		}
	}

	// Mount/scope cycle: the $effect teardown is the release path.
	$effect(() => {
		void services;
		void conversationId;
		void serverId;
		void POLL_INTERVAL;
		disposed = false;
		void load();
		const timer =
			POLL_INTERVAL > 0
				? setInterval(() => {
						if (!disposed) void syncTick();
					}, POLL_INTERVAL)
				: null;
		return () => {
			disposed = true;
			epoch += 1;
			if (timer !== null) clearInterval(timer);
		};
	});

	async function loadMoreSteps(): Promise<void> {
		const api = usableApi(boundServices);
		const current = header;
		if (api === null || current === null || loadingMoreSteps) return;
		loadingMoreSteps = true;
		try {
			const page = await api.listTaskSteps(current.task_id, current.server_id, {
				limit: PAGE_LIMIT,
				offset: steps.length
			});
			if (disposed || header === null) return;
			steps = appendUnique(steps, page.items ?? [], (item) => item.step_id);
			stepsHasMore = page.has_more;
			stepsTotal = page.total;
		} catch {
			// Honest stop: the page stays as-is; hasMore stays truthful.
		} finally {
			loadingMoreSteps = false;
		}
	}

	async function loadMoreEvidence(): Promise<void> {
		const api = usableApi(boundServices);
		const current = header;
		if (api === null || current === null || loadingMoreEvidence) return;
		loadingMoreEvidence = true;
		try {
			const page = await api.listTaskEvidence(current.task_id, current.server_id, {
				limit: PAGE_LIMIT,
				offset: evidence.length
			});
			if (disposed || header === null) return;
			evidence = appendUnique(evidence, page.items ?? [], (item) => item.evidence_id);
			evidenceHasMore = page.has_more;
			evidenceTotal = page.total;
		} catch {
			// Honest stop.
		} finally {
			loadingMoreEvidence = false;
		}
	}

	async function loadMoreRevisions(): Promise<void> {
		const api = usableApi(boundServices);
		const current = header;
		if (api === null || current === null || loadingMoreRevisions) return;
		loadingMoreRevisions = true;
		try {
			const page = await api.listTaskRevisions(current.task_id, current.server_id, {
				limit: PAGE_LIMIT,
				offset: revisions.length
			});
			if (disposed || header === null) return;
			revisions = appendUnique(revisions, page.items ?? [], (item) => String(item.revision));
			revisionsHasMore = page.has_more;
			revisionsTotal = page.total;
		} catch {
			// Honest stop.
		} finally {
			loadingMoreRevisions = false;
		}
	}

	// --- value-free display helpers (C08 rule 6) -----------------------------

	const STATUS_TEXT: Record<string, string> = {
		pending: 'pending',
		in_progress: 'in progress',
		blocked: 'blocked',
		executed: 'executed',
		skipped: 'skipped'
	};

	const VERIFICATION_TEXT: Record<string, string> = {
		not_checked: 'not checked',
		passed: 'verified',
		failed: 'failed',
		unavailable: 'unavailable'
	};

	const OUTCOME_TEXT: Record<string, string> = {
		verified: 'verified',
		partially_verified: 'partially verified',
		in_progress: 'in progress',
		needs_input: 'needs input',
		failed: 'failed',
		cancelled: 'cancelled'
	};

	const progressMeta = $derived.by<string | null>(() => {
		if (header === null) return null;
		const counts = header.counts;
		return (
			`phase ${header.requested_phase} (advisory) · ` +
			`${OUTCOME_TEXT[header.status] ?? header.status} · rev ${header.revision} · ` +
			`steps ${counts.steps_executed}/${counts.steps_total} executed · ` +
			`criteria ${counts.criteria_verified}/${counts.criteria_total} verified · ` +
			`constraints ${counts.constraints_total} · evidence ${counts.evidence_total}`
		);
	});

	// --- A4 legacy-stuck-task recovery (agent-interaction-recovery) ----------

	type RecoveryState = 'idle' | 'pending' | 'submitted' | 'error';

	let recoveryState = $state<RecoveryState>('idle');
	let recoveryDetail = $state<string | null>(null);

	/**
	 * The recovery action exists ONLY for an OPEN episode already stranded
	 * in the `inspect` phase that cannot advance on its own. A direct
	 * simple operation is accepted `act` from the outset (A5) and never
	 * sees this block; read-only mode never offers it; a terminal episode
	 * is never resurrected by it. The callback's absence keeps the block
	 * off (hosts that do not wire the canonical submission).
	 */
	const recoveryVisible = $derived.by(() => {
		if (readOnly || onContinueWithChanges === undefined) return false;
		if (uiState !== 'ready' || header === null) return false;
		if (header.requested_phase !== 'inspect') return false;
		return header.status === 'in_progress' || header.status === 'needs_input';
	});

	function recoveryProblemText(error: unknown): string {
		if (error instanceof ApiError && error.code === 'revision_conflict') {
			return 'The task changed while submitting (stale revision). Nothing was changed; review the current plan, then retry.';
		}
		if (error instanceof ApiError) {
			return `The recovery turn was not recorded (${error.code}). Nothing was changed; no success is claimed.`;
		}
		return 'The recovery turn could not be recorded. Nothing was changed; no success is claimed.';
	}

	async function continueWithChanges(): Promise<void> {
		if (!recoveryVisible || header === null || recoveryState === 'pending') return;
		// Scope and revision are EXPLICIT at the handover: the parent submits
		// the canonical follow-up against THIS pinned scope and revision.
		const expectedRevision = header.revision;
		recoveryState = 'pending';
		recoveryDetail = null;
		try {
			await onContinueWithChanges?.(expectedRevision);
			// Honest outcome: the TURN was admitted — nothing about the task
			// outcome is claimed, and no operation was approved by this click.
			recoveryState = 'submitted';
			recoveryDetail = null;
		} catch (error) {
			recoveryState = 'error';
			recoveryDetail = recoveryProblemText(error);
		}
	}

	function statusText(status: string): string {
		return STATUS_TEXT[status] ?? status;
	}

	function verificationText(verification: string): string {
		return VERIFICATION_TEXT[verification] ?? verification;
	}

	function stepDetail(step: TaskStepView): string {
		const parts: string[] = [step.risk];
		if (step.prerequisite_unresolved) parts.push('prerequisite unresolved');
		if (step.skip_reason !== null && step.skip_reason !== '') parts.push(`skip reason: ${step.skip_reason}`);
		if (step.result_summary !== null && step.result_summary !== '') {
			parts.push(`result: ${step.result_summary}`);
		}
		return parts.join(' · ');
	}

	function dependsText(step: TaskStepView): string | null {
		if (step.depends_on.length === 0) return null;
		return `depends on ${step.depends_on.join(', ')}`;
	}

	function evidenceShortId(id: string): string {
		return id.length > 13 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
	}

	// --- proposal construction (bounded, contract-valid) ----------------------

	const targetStep = $derived.by<TaskStepView | null>(() => {
		if (proposalOp === 'add') return null;
		return steps.find((step) => step.step_id === targetStepId) ?? null;
	});

	/** The item under removal is RUNNING: the honest non-stop warning. */
	const targetRunning = $derived(targetStep !== null && targetStep.status === 'in_progress');

	const proposalNeedsTitle = $derived(proposalOp === 'add' || proposalOp === 'update');

	const proposalReady = $derived.by(() => {
		if (proposalState === 'pending') return false;
		if (sourceMessageId === null || sourceMessageId === '') return false;
		if (header === null) return false;
		if (proposalNeedsTitle && proposalTitle.trim() === '') return false;
		if (proposalOp !== 'add' && targetStep === null) return false;
		return true;
	});

	function proposalOperations(): TaskStepOperation[] {
		const current = targetStep;
		if (proposalOp === 'add') {
			return [
				{
					op: 'add',
					step: {
						step_id: crypto.randomUUID(),
						title: proposalTitle.trim(),
						status: 'pending',
						verification: 'not_checked',
						risk: 'read_only',
						prerequisite_unresolved: false,
						expected_check: null,
						skip_reason: null,
						depends_on: [],
						evidence_refs: []
					}
				}
			];
		}
		if (current === null) return [];
		const carried = {
			title: proposalOp === 'update' ? proposalTitle.trim() : current.title,
			status: current.status,
			verification: current.verification,
			risk: current.risk,
			prerequisite_unresolved: current.prerequisite_unresolved,
			expected_check: current.expected_check,
			skip_reason: current.skip_reason,
			depends_on: [...current.depends_on],
			evidence_refs: current.evidence_refs.map((ref) => ({ ...ref }))
		};
		if (proposalOp === 'update') {
			return [{ op: 'update', step: { step_id: current.step_id, ...carried } }];
		}
		// Removal is a SUPERSEDE: the replaced row stays recorded server-side
		// (P3 history); the replacement is the explicit skipped row.
		return [
			{
				op: 'supersede',
				supersedes_step_id: current.step_id,
				step: {
					step_id: crypto.randomUUID(),
					...carried,
					status: 'skipped',
					skip_reason: `removed by user revision proposal (was ${statusText(current.status)})`
				}
			}
		];
	}

	async function submitProposal(): Promise<void> {
		const api = usableApi(boundServices);
		const current = header;
		const anchor = sourceMessageId;
		if (api === null || current === null || anchor === null || anchor === '' || !proposalReady) {
			return;
		}
		const operations = proposalOperations();
		if (operations.length === 0) return;
		proposalState = 'pending';
		proposalOutcome = null;
		proposalProblem = null;
		try {
			const outcome = await api.submitTaskCorrection(current.task_id, {
				server_id: current.server_id,
				patch: {
					schema_version: 1,
					source_message_id: anchor,
					base_revision: current.revision,
					idempotency_key: crypto.randomUUID(),
					rationale: proposalRationale.trim() === '' ? null : proposalRationale.trim(),
					operations
				}
			});
			if (disposed) return;
			proposalOutcome = {
				revision: outcome.revision.revision,
				disposition: outcome.disposition
			};
			proposalState = 'applied';
			// The server's fresh header IS the shared truth (P5): adopt it and
			// re-read the collections under the new revision.
			header = outcome.task;
			await reloadPages(outcome.task);
		} catch (error) {
			if (disposed) return;
			if (error instanceof ApiError && error.status === 409) {
				// The stale compare-and-swap: NOTHING was overwritten.
				proposalState = 'conflict';
				proposalProblem = { status: error.status, code: error.code, message: error.message };
			} else {
				proposalState = 'error';
				proposalProblem = {
					status: error instanceof ApiError ? error.status : 0,
					code: error instanceof ApiError ? error.code : 'unavailable',
					message: error instanceof Error && error.message !== '' ? error.message : 'request failed'
				};
			}
		}
	}

	function selectOp(op: string): void {
		if (op !== 'add' && op !== 'update' && op !== 'supersede') return;
		proposalOp = op;
		proposalState = 'idle';
		proposalOutcome = null;
		proposalProblem = null;
		if (op === 'update' && targetStepId === '') {
			const firstEditable = steps.find((step) => step.superseded_at === null);
			targetStepId = firstEditable?.step_id ?? '';
		}
	}

	function proposalOutcomeText(): string {
		if (proposalState === 'pending') {
			return 'Proposal pending — awaiting the server’s revision acknowledgement.';
		}
		if (proposalState === 'applied' && proposalOutcome !== null) {
			return proposalOutcome.disposition === 'deduplicated'
				? `Already recorded (deduplicated) — revision ${proposalOutcome.revision} of the append-only ledger.`
				: `Proposal applied as revision ${proposalOutcome.revision}; the previous rows stay recorded in the ledger.`;
		}
		if (proposalState === 'conflict') {
			return 'Revision conflict — the task changed while this proposal was open (server code: revision_conflict). Nothing was overwritten; refresh the plan and re-apply the change.';
		}
		if (proposalState === 'error' && proposalProblem !== null) {
			return `Proposal could not be recorded (${proposalProblem.code}).`;
		}
		return '';
	}
</script>

{#if uiState === 'ready' && header !== null}
	<section class="mai-task-plan" data-testid="task-plan" data-state="ready" aria-label="Task plan">
		<div class="mai-task-plan__scope" data-testid="task-plan-scope">
			<span class="mai-task-plan__title" data-testid="task-plan-title">{header.title}</span>
			<span class="mai-task-plan__meta" data-testid="task-plan-progress">{progressMeta}</span>
			<Button
				variant="secondary"
				size="sm"
				loading={syncing}
				onclick={syncTick}
				data-testid="task-plan-refresh"
			>
				Refresh plan
			</Button>
		</div>

		{#if recoveryVisible}
			<div class="mai-task-plan__recovery" data-testid="task-plan-recovery" data-state={recoveryState}>
				<p class="mai-task-plan__note" data-testid="task-plan-recovery-explainer">
					This task is waiting in the inspect phase and cannot advance on its own.
					Continuing with changes moves it to the act phase — the next steps
					will change target state. This does not approve any operation:
					existing grants and approvals still apply.
				</p>
				<Button
					variant="secondary"
					size="sm"
					loading={recoveryState === 'pending'}
					disabled={recoveryState === 'pending'}
					onclick={continueWithChanges}
					data-testid="task-plan-recovery-continue"
				>
					Continue with changes
				</Button>
				{#if recoveryState === 'submitted'}
					<p class="mai-task-plan__note" data-testid="task-plan-recovery-submitted" data-state="submitted">
						Recovery turn submitted; the run continues this task. No operation
						has been approved and no outcome is claimed yet.
					</p>
				{:else if recoveryState === 'error' && recoveryDetail !== null}
					<p
						class="mai-task-plan__warning"
						data-testid="task-plan-recovery-problem"
						data-state="error"
					>
						{recoveryDetail}
					</p>
				{/if}
			</div>
		{/if}

		<div class="mai-task-plan__section" data-testid="task-plan-steps" role="group" aria-label="Plan items">
			<h4 class="mai-task-plan__heading">Plan items</h4>
			{#if steps.length === 0}
				<p class="mai-task-plan__gap" data-testid="task-plan-steps-empty">No plan items recorded.</p>
			{:else}
				<ul class="mai-task-plan__list" data-testid="task-plan-step-list">
					{#each steps as step (step.step_id)}
						<li
							class="mai-task-plan__row"
							data-testid="task-plan-step-row"
							data-step-id={step.step_id}
							data-step-status={step.status}
							data-step-verification={step.verification}
						>
							<span class="mai-task-plan__row-key">
								{step.position}. {step.title}{#if header.active_step_id === step.step_id}<span
										class="mai-task-plan__active"> (active)</span
									>{/if}
							</span>
							<span class="mai-task-plan__badges">
								<span
									class="mai-task-plan__badge mai-task-plan__badge--status mai-task-plan__badge--status-{step.status}"
									data-testid="task-plan-step-status"
									data-state={step.status}>{statusText(step.status)}</span
								>
								<span
									class="mai-task-plan__badge mai-task-plan__badge--verification mai-task-plan__badge--verification-{step.verification}"
									data-testid="task-plan-step-verification"
									data-state={step.verification}>{verificationText(step.verification)}</span
								>
							</span>
							<span class="mai-task-plan__row-text">
								{stepDetail(step)}
								{#if step.evidence_refs.length > 0}
									<span class="mai-task-plan__step-evidence" data-testid="task-plan-step-evidence">
										evidence: {step.evidence_refs
											.map((ref) => `${ref.kind} ${evidenceShortId(ref.evidence_id)}`)
											.join(', ')}
									</span>
								{/if}
								{#if dependsText(step) !== null}
									<span class="mai-task-plan__step-deps" data-testid="task-plan-step-deps">
										{dependsText(step)}
									</span>
								{/if}
							</span>
						</li>
					{/each}
				</ul>
				<p class="mai-task-plan__meta" data-testid="task-plan-steps-meta">
					Showing {steps.length} of {stepsTotal} items.
				</p>
				{#if stepsHasMore}
					<Button
						variant="secondary"
						size="sm"
						loading={loadingMoreSteps}
						onclick={loadMoreSteps}
						data-testid="task-plan-steps-more"
					>
						Load more items
					</Button>
				{/if}
			{/if}
		</div>

		<div class="mai-task-plan__section" data-testid="task-plan-evidence" role="group" aria-label="Evidence links">
			<h4 class="mai-task-plan__heading">Evidence links</h4>
			{#if evidence.length === 0}
				<p class="mai-task-plan__gap" data-testid="task-plan-evidence-empty">
					No evidence records linked.
				</p>
			{:else}
				<ul class="mai-task-plan__list" data-testid="task-plan-evidence-list">
					{#each evidence as record (record.evidence_id)}
						<li class="mai-task-plan__row" data-testid="task-plan-evidence-row">
							<span class="mai-task-plan__row-key">
								{record.kind} {record.evidence_id}
							</span>
							<span class="mai-task-plan__row-text">
								{#if record.note !== null && record.note !== ''}{record.note} · {/if}
								{#if record.origin_step_id !== null && record.origin_step_id !== ''}from item {record.origin_step_id} · {/if}
								registered {record.created_at}
							</span>
						</li>
					{/each}
				</ul>
				<p class="mai-task-plan__meta" data-testid="task-plan-evidence-meta">
					Showing {evidence.length} of {evidenceTotal} records.
				</p>
				{#if evidenceHasMore}
					<Button
						variant="secondary"
						size="sm"
						loading={loadingMoreEvidence}
						onclick={loadMoreEvidence}
						data-testid="task-plan-evidence-more"
					>
						Load more evidence
					</Button>
				{/if}
			{/if}
		</div>

		<div class="mai-task-plan__section" data-testid="task-plan-revisions" role="group" aria-label="Revision ledger">
			<h4 class="mai-task-plan__heading">Revision ledger</h4>
			{#if revisions.length === 0}
				<p class="mai-task-plan__gap" data-testid="task-plan-revisions-empty">
					No revisions recorded.
				</p>
			{:else}
				<ul class="mai-task-plan__list" data-testid="task-plan-revision-list">
					{#each revisions as revision (revision.revision)}
						<li class="mai-task-plan__row" data-testid="task-plan-revision-row">
							<span class="mai-task-plan__row-key">rev {revision.revision} · {revision.kind}</span>
							<span class="mai-task-plan__row-text">
								by {revision.author}
								{#if revision.item_id !== null && revision.item_id !== ''} · item {revision.item_id}{/if}
								{#if revision.source_message_id !== null && revision.source_message_id !== ''}
									· source message {revision.source_message_id}
								{/if}
								· applied {revision.applied_at}
							</span>
						</li>
					{/each}
				</ul>
				<p class="mai-task-plan__meta" data-testid="task-plan-revisions-meta">
					Showing {revisions.length} of {revisionsTotal} revisions (newest first).
				</p>
				{#if revisionsHasMore}
					<Button
						variant="secondary"
						size="sm"
						loading={loadingMoreRevisions}
						onclick={loadMoreRevisions}
						data-testid="task-plan-revisions-more"
					>
						Load more revisions
					</Button>
				{/if}
			{/if}
		</div>

		<div class="mai-task-plan__section" data-testid="task-plan-proposal" role="group" aria-label="Propose a plan revision">
			<h4 class="mai-task-plan__heading">Propose a revision</h4>
			{#if sourceMessageId === null || sourceMessageId === ''}
				<p class="mai-task-plan__gap" data-testid="task-plan-proposal-unanchored">
					Proposals anchor to a recorded user message of this conversation; none is
					available yet, so proposing is disabled. Nothing is fabricated in its place.
				</p>
			{:else}
				<p class="mai-task-plan__note" data-testid="task-plan-proposal-policy">
					Edits are PROPOSALS under compare-and-swap: they append a new revision and never
					rewrite or delete recorded history. Removal supersedes an item with a skipped row;
					the original stays in the ledger.
				</p>
				<div class="mai-task-plan__form">
					<Select
						label="Change"
						data-testid="task-plan-proposal-op"
						value={proposalOp}
						onchange={(event) => selectOp((event.currentTarget as HTMLSelectElement).value)}
					>
						<option value="add">Add an item</option>
						<option value="update">Rename an item</option>
						<option value="supersede">Remove an item (supersede)</option>
					</Select>
					{#if proposalOp !== 'add'}
						<Select label="Item" data-testid="task-plan-proposal-target" bind:value={targetStepId}>
							<option value="">Select an item…</option>
							{#each steps as step (step.step_id)}
								<option value={step.step_id}>
									{step.position}. {step.title} ({statusText(step.status)})
								</option>
							{/each}
						</Select>
					{/if}
					{#if proposalNeedsTitle}
						<Field
							label={proposalOp === 'add' ? 'New item title' : 'Replacement title'}
							data-testid="task-plan-proposal-title"
							bind:value={proposalTitle}
							maxlength={240}
						/>
					{/if}
					<Field
						label="Why (optional)"
						data-testid="task-plan-proposal-rationale"
						bind:value={proposalRationale}
						maxlength={1000}
					/>
					{#if targetRunning}
						<p class="mai-task-plan__warning" data-testid="task-plan-running-warning">
							This item is in progress. Removal is a proposal only — it does NOT stop any
							running process; the run reports its real state, and the executor acknowledges
							the change at its next safe boundary.
						</p>
					{/if}
					{#if proposalState !== 'idle'}
						<p
							class="mai-task-plan__outcome"
							data-testid="task-plan-proposal-outcome"
							data-state={proposalState}
						>
							{proposalOutcomeText()}
							{#if proposalState === 'error' && proposalProblem !== null}
								<span class="mai-task-plan__meta">
									<span data-testid="task-plan-proposal-error-code">{proposalProblem.code}</span>
									{proposalProblem.message}
								</span>
							{/if}
						</p>
					{/if}
					<Button
						variant="primary"
						size="sm"
						loading={proposalState === 'pending'}
						disabled={!proposalReady}
						onclick={submitProposal}
						data-testid="task-plan-proposal-submit"
					>
						Submit proposal
					</Button>
				</div>
			{/if}
		</div>
	</section>
{:else}
	<section
		class="mai-task-plan mai-task-plan--flat"
		data-testid="task-plan"
		data-state={uiState}
		aria-label="Task plan"
	>
		{#if uiState === 'loading'}
			<p class="mai-task-plan__gap" data-testid="task-plan-loading">Loading task plan…</p>
		{:else if uiState === 'unavailable'}
			<p class="mai-task-plan__gap" data-testid="task-plan-unavailable">
				Task plan unavailable.
				<span class="mai-task-plan__meta">
					No active task episode is recorded for this conversation in this server scope.
				</span>
			</p>
		{:else if uiState === 'error'}
			<p class="mai-task-plan__gap" data-testid="task-plan-error">
				Task plan could not be loaded.
				<span class="mai-task-plan__meta">
					<span data-testid="task-plan-error-code">{problem?.code}</span>
					{problem?.message}
				</span>
			</p>
		{:else}
			<p class="mai-task-plan__gap" data-testid="task-plan-unbound">
				Task plan unavailable.
				<span class="mai-task-plan__meta">
					No task services are bound; nothing is fetched or fabricated.
				</span>
			</p>
		{/if}
	</section>
{/if}

<style>
	.mai-task-plan {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 8px);
		margin: var(--mai-space-2, 8px) 0;
		padding: var(--mai-space-2, 8px);
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--mai-radius-sm, 4px);
		background: var(--mai-color-surface-raised, transparent);
		max-width: 100%;
		min-width: 0;
	}
	.mai-task-plan--flat {
		padding: var(--mai-space-2, 8px);
	}
	.mai-task-plan__scope {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--mai-space-1, 4px);
		min-width: 0;
	}
	.mai-task-plan__title {
		font-size: var(--mai-text-sm, 0.875rem);
		font-weight: var(--mai-weight-semibold, 600);
		color: var(--mai-color-text, inherit);
		overflow-wrap: anywhere;
	}
	.mai-task-plan__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 4px);
		min-width: 0;
	}
	.mai-task-plan__heading {
		margin: 0;
		font-size: var(--mai-text-xs, 0.75rem);
		font-weight: var(--mai-weight-semibold, 600);
		color: var(--mai-color-text, inherit);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.mai-task-plan__list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 4px);
		max-height: 16rem;
		overflow-y: auto;
		min-width: 0;
	}
	.mai-task-plan__row {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: var(--mai-text-xs, 0.75rem);
		color: var(--mai-color-text-muted, inherit);
		overflow-wrap: anywhere;
		min-width: 0;
	}
	.mai-task-plan__row-key {
		font-weight: var(--mai-weight-semibold, 600);
		color: var(--mai-color-text, inherit);
		overflow-wrap: anywhere;
	}
	.mai-task-plan__row-text {
		overflow-wrap: anywhere;
	}
	.mai-task-plan__badges {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-1, 4px);
		min-width: 0;
	}
	.mai-task-plan__badge {
		display: inline-block;
		padding: 0 var(--mai-space-1, 4px);
		border-radius: var(--mai-radius-sm, 4px);
		border: 1px solid var(--mai-color-border, transparent);
		font-size: var(--mai-text-xs, 0.75rem);
		font-family: var(--mai-font-mono, monospace);
		overflow-wrap: anywhere;
		white-space: nowrap;
	}
	/* Executed: work happened (outlined, neutral) — NOT a completion verdict. */
	.mai-task-plan__badge--status-executed {
		border-color: var(--mai-color-border-strong, currentColor);
		color: var(--mai-color-text, inherit);
		background: var(--mai-color-bg, transparent);
		font-weight: var(--mai-weight-semibold, 600);
	}
	/* Verified: the agreed check passed (filled) — a distinct axis + look. */
	.mai-task-plan__badge--verification-passed {
		border-color: var(--mai-color-brass, currentColor);
		background: var(--mai-color-brass, transparent);
		color: var(--mai-color-bg, inherit);
		font-weight: var(--mai-weight-semibold, 600);
	}
	.mai-task-plan__badge--verification-failed {
		border-style: dashed;
		border-color: var(--mai-color-border-strong, currentColor);
		font-weight: var(--mai-weight-semibold, 600);
	}
	.mai-task-plan__badge--status-skipped,
	.mai-task-plan__badge--status-blocked {
		border-style: dashed;
	}
	.mai-task-plan__active {
		color: var(--mai-color-text-muted, inherit);
		font-style: italic;
	}
	.mai-task-plan__step-evidence,
	.mai-task-plan__step-deps {
		display: block;
		font-family: var(--mai-font-mono, monospace);
	}
	.mai-task-plan__gap {
		margin: 0;
		color: var(--mai-color-text, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
		overflow-wrap: anywhere;
	}
	.mai-task-plan__meta {
		color: var(--mai-color-text-muted, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
		overflow-wrap: anywhere;
	}
	.mai-task-plan__note {
		margin: 0;
		color: var(--mai-color-text-muted, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
		overflow-wrap: anywhere;
	}
	/* A4 recovery: a bounded, visually distinct affordance for a legacy
	   inspect-stranded episode only — never a normal-path gate. */
	.mai-task-plan__recovery {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--mai-space-2, 8px);
		padding: var(--mai-space-2, 8px);
		border: 1px dashed var(--mai-color-border-strong, currentColor);
		border-radius: var(--mai-radius-sm, 4px);
		min-width: 0;
		max-width: 100%;
	}
	.mai-task-plan__warning {
		margin: 0;
		padding: var(--mai-space-1, 4px);
		border: 1px dashed var(--mai-color-border-strong, currentColor);
		border-radius: var(--mai-radius-sm, 4px);
		color: var(--mai-color-text, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
		overflow-wrap: anywhere;
	}
	.mai-task-plan__outcome {
		margin: 0;
		font-size: var(--mai-text-xs, 0.75rem);
		overflow-wrap: anywhere;
	}
	.mai-task-plan__outcome[data-state='pending'] {
		color: var(--mai-color-text-muted, inherit);
	}
	.mai-task-plan__outcome[data-state='applied'] {
		color: var(--mai-color-text, inherit);
		font-weight: var(--mai-weight-semibold, 600);
	}
	.mai-task-plan__outcome[data-state='conflict'],
	.mai-task-plan__outcome[data-state='error'] {
		color: var(--mai-color-text, inherit);
		font-weight: var(--mai-weight-semibold, 600);
	}
	.mai-task-plan__form {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 8px);
		min-width: 0;
		max-width: 100%;
	}
</style>
