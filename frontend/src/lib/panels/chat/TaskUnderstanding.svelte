<script lang="ts">
	/**
	 * TaskUnderstanding — compact, read-only §8 understanding presentation
	 * (act `17-understanding-ui`; act_CONTRACTS §8 "Understanding" section).
	 *
	 * Renders the ONE active task episode of a conversation as a compact
	 * block: the scope (title, summary, advisory phase, domain status,
	 * revision, counts), the active constraints, the open questions and the
	 * proposed plan (live steps). All data is fetched READ-ONLY through the
	 * injected `07-web-tasks.ts` service boundary — the block never mutates:
	 * no revision, no decision, no step-status change on open (criterion 2).
	 *
	 * Honesty rules (C08 rule 6; AGENTS.md §1):
	 * - Every uiState is explicit and fail-closed: an unbound/incomplete
	 *   services bag, an absent episode (the scope-blind 404) and a transport
	 *   error each render their OWN label — nothing is ever fabricated.
	 * - The §8 wire exposes NO open-questions collection, so the block
	 *   renders the explicit "no open questions recorded" uiState instead of
	 *   inventing questions (documented gap; a future wire extension slots
	 *   in here).
	 * - Constraints and steps are bounded pages: a fixed page inside an
	 *   `overflow-y: auto` container, a "Showing n of m" line and a "Load
	 *   more" control whenever more remain — never an unbounded dump.
	 *   Appended pages are deduplicated by stable id.
	 * - Zero browser storage: the server is the only source of task truth,
	 *   so reopening/reconnecting re-derives the same understanding (P5).
	 *
	 * Svelte 5 runes only; no module-level uiState; tokens only (ADR §5 rule 4).
	 */
	import Button from '../../ui/Button.svelte';
	import { ApiError } from '../../api/client.js';
	import {
		TASK_UNDERSTANDING_PAGE_LIMIT,
		loadTaskUnderstandingSections
	} from '../../workspace/entries/07-web-tasks.js';
	import type {
		TaskApi,
		TaskConstraintView,
		TaskServices,
		TaskStepView,
		TaskUnderstandingSections,
		TaskView
	} from '../../workspace/entries/07-web-tasks.js';

	/**
	 * The honest render uiState. `unbound` = no usable task api slice bound
	 * (nothing fetched, nothing fabricated); `unavailable` = the scope-blind
	 * 404 — no active task episode for this conversation in this scope;
	 * `error` = any other transport failure; `ready` = server-backed data.
	 * (Declared before any rune-initialized statement: svelte2tsx misorders
	 * type aliases interleaved after them.)
	 */
	export type UnderstandingState = 'unbound' | 'loading' | 'unavailable' | 'error' | 'ready';

	interface Props {
		/** The task service bag from the opener; a missing bag fails closed. */
		services: TaskServices | null;
		/** Conversation whose ONE active task episode is presented. */
		conversationId: string | null;
		/** Explicit server scope (C01 rule 2). */
		serverId: string | null;
		/** Bounded page size override (tests); server bounds still apply. */
		pageSize?: number;
		/**
		 * Optional parent binding (cti-01 Task Inspector drawer): mirrors
		 * `uiState` upward so the host trigger/badge reads the honest render
		 * state without any second network read. Absent binding ⇒ local only.
		 */
		status?: UnderstandingState;
		/** Optional parent binding: mirrors the ready task header (badge counts). */
		taskHeader?: TaskView | null;
	}

	let {
		services,
		conversationId,
		serverId,
		pageSize = TASK_UNDERSTANDING_PAGE_LIMIT,
		status = $bindable<UnderstandingState>('unbound'),
		taskHeader = $bindable<TaskView | null>(null)
	}: Props = $props();

	/** Bounded section page size (AGENTS.md §1). */
	const PAGE_LIMIT = $derived(pageSize > 0 ? pageSize : TASK_UNDERSTANDING_PAGE_LIMIT);

	let uiState = $state<UnderstandingState>('unbound');
	let problem = $state<{ status: number; code: string; message: string } | null>(null);
	let sections = $state<TaskUnderstandingSections | null>(null);

	// cti-01: one-way mirror onto the optional parent bindings — the Task
	// Inspector drawer reads the honest state without duplicating a single
	// fetch (C08 rule 6). The effect only writes; it never reads the bindings.
	$effect(() => {
		status = uiState;
		taskHeader = sections?.header ?? null;
	});

	/** Deduplicated, append-only section pages (AGENTS.md §1). */
	let constraints = $state<readonly TaskConstraintView[]>([]);
	let constraintsTotal = $state(0);
	let constraintsHasMore = $state(false);
	let loadingMoreConstraints = $state(false);
	let steps = $state<readonly TaskStepView[]>([]);
	let stepsTotal = $state(0);
	let stepsHasMore = $state(false);
	let loadingMoreSteps = $state(false);

	/** Stale-response guard: only the newest load may write uiState (C08 rule 6). */
	let epoch = 0;
	/** Set once the mount cycle ended; async continuations check it (ADR §7.2). */
	let disposed = true;

	/** The api slice this block consumes; an incomplete bag fails closed. */
	function usableApi(bag: TaskServices | null): TaskApi | null {
		if (bag === null) return null;
		const api: TaskApi | undefined = bag.api;
		if (api === null || typeof api !== 'object') return null;
		if (
			typeof api.getConversationTask !== 'function' ||
			typeof api.listTaskConstraints !== 'function' ||
			typeof api.listTaskSteps !== 'function'
		) {
			return null;
		}
		return api;
	}

	async function load(): Promise<void> {
		const api = usableApi(services);
		if (api === null || conversationId === null || conversationId === '' || serverId === null || serverId === '') {
			uiState = 'unbound';
			sections = null;
			problem = null;
			constraints = [];
			steps = [];
			return;
		}
		const currentEpoch = ++epoch;
		uiState = 'loading';
		problem = null;
		try {
			const next = await loadTaskUnderstandingSections(api, conversationId, serverId);
			if (disposed || currentEpoch !== epoch) return;
			sections = next;
			constraints = [...next.constraints];
			constraintsTotal = next.constraintsTotal;
			constraintsHasMore = next.constraintsHasMore;
			steps = [...next.steps];
			stepsTotal = next.stepsTotal;
			stepsHasMore = next.stepsHasMore;
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

	// Mount/scope cycle: the $effect teardown is the release path.
	$effect(() => {
		void services;
		void conversationId;
		void serverId;
		disposed = false;
		void load();
		return () => {
			disposed = true;
			epoch += 1;
		};
	});

	function appendUnique<T>(
		current: readonly T[],
		incoming: readonly T[],
		id: (item: T) => string
	): T[] {
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

	async function loadMoreConstraints(): Promise<void> {
		const api = usableApi(services);
		if (api === null || sections === null || loadingMoreConstraints) return;
		loadingMoreConstraints = true;
		try {
			const page = await api.listTaskConstraints(sections.header.task_id, sections.header.server_id, {
				limit: PAGE_LIMIT,
				offset: constraints.length
			});
			if (disposed || sections === null) return;
			constraints = appendUnique(constraints, page.items ?? [], (item) => item.constraint_id);
			constraintsHasMore = page.has_more;
			constraintsTotal = page.total;
		} catch {
			// Honest stop: the page stays as-is; hasMore stays truthful.
		} finally {
			loadingMoreConstraints = false;
		}
	}

	async function loadMoreSteps(): Promise<void> {
		const api = usableApi(services);
		if (api === null || sections === null || loadingMoreSteps) return;
		loadingMoreSteps = true;
		try {
			const page = await api.listTaskSteps(sections.header.task_id, sections.header.server_id, {
				limit: PAGE_LIMIT,
				offset: steps.length
			});
			if (disposed || sections === null) return;
			steps = appendUnique(steps, page.items ?? [], (item) => item.step_id);
			stepsHasMore = page.has_more;
			stepsTotal = page.total;
		} catch {
			// Honest stop: the page stays as-is; hasMore stays truthful.
		} finally {
			loadingMoreSteps = false;
		}
	}

	// --- value-free display helpers (C08 rule 6) -----------------------------

	const STATUS_TEXT: Record<string, string> = {
		verified: 'verified',
		partially_verified: 'partially verified',
		in_progress: 'in progress',
		needs_input: 'needs input',
		failed: 'failed',
		cancelled: 'cancelled'
	};

	const VERIFICATION_TEXT: Record<string, string> = {
		not_checked: 'not checked',
		passed: 'passed',
		failed: 'failed',
		unavailable: 'unavailable'
	};

	const statusText = $derived.by<string | null>(() => {
		if (sections === null) return null;
		const raw = sections.header.status;
		return STATUS_TEXT[raw] ?? raw;
	});

	const scopeMeta = $derived.by<string | null>(() => {
		if (sections === null) return null;
		const header = sections.header;
		const counts = header.counts;
		return (
			`phase ${header.requested_phase} (advisory) · ` +
			`${STATUS_TEXT[header.status] ?? header.status} · rev ${header.revision} · ` +
			`steps ${counts.steps_executed}/${counts.steps_total} executed · ` +
			`constraints ${counts.constraints_total} · evidence ${counts.evidence_total} · ` +
			`criteria ${counts.criteria_verified}/${counts.criteria_total} verified`
		);
	});

	function stepStatusText(status: string): string {
		if (status === 'in_progress') return 'in progress';
		return status;
	}

	function stepDetail(step: TaskStepView): string {
		const parts: string[] = [
			`${stepStatusText(step.status)} · ${VERIFICATION_TEXT[step.verification] ?? step.verification} · ${step.risk}`
		];
		if (step.prerequisite_unresolved) parts.push('prerequisite unresolved');
		if (step.skip_reason !== null && step.skip_reason !== '') parts.push(`skip reason: ${step.skip_reason}`);
		return parts.join(' · ');
	}
</script>

{#if uiState === 'ready' && sections !== null}
	<section class="mai-task-understanding" data-testid="task-understanding" data-state="ready" aria-label="Task understanding">
		<div class="mai-task-understanding__scope" data-testid="task-understanding-scope">
			<span class="mai-task-understanding__title" data-testid="task-understanding-scope-title">
				{sections.header.title}
			</span>
			{#if sections.header.summary === null || sections.header.summary === ''}
				<span class="mai-task-understanding__gap" data-testid="task-understanding-scope-summary">
					No summary recorded.
				</span>
			{:else}
				<span class="mai-task-understanding__summary" data-testid="task-understanding-scope-summary">
					{sections.header.summary}
				</span>
			{/if}
			<span class="mai-task-understanding__meta" data-testid="task-understanding-scope-meta">{scopeMeta}</span>
		</div>

		<div class="mai-task-understanding__section" data-testid="task-understanding-constraints" role="group" aria-label="Task constraints">
			<h4 class="mai-task-understanding__heading">Constraints</h4>
			{#if constraints.length === 0}
				<p class="mai-task-understanding__gap" data-testid="task-understanding-constraints-empty">
					No constraints recorded.
				</p>
			{:else}
				<ul class="mai-task-understanding__list" data-testid="task-understanding-constraint-list">
					{#each constraints as constraint (constraint.constraint_id)}
						<li class="mai-task-understanding__row" data-testid="task-understanding-constraint-row">
							<span class="mai-task-understanding__row-key">{constraint.category}</span>
							<span class="mai-task-understanding__row-text">
								{constraint.statement}
								<span class="mai-task-understanding__origin">({constraint.origin})</span>
							</span>
						</li>
					{/each}
				</ul>
				<p class="mai-task-understanding__meta" data-testid="task-understanding-constraints-meta">
					Showing {constraints.length} of {constraintsTotal} constraints.
				</p>
				{#if constraintsHasMore}
					<Button
						variant="secondary"
						size="sm"
						loading={loadingMoreConstraints}
						onclick={loadMoreConstraints}
						data-testid="task-understanding-constraints-more"
					>
						Load more constraints
					</Button>
				{/if}
			{/if}
		</div>

		<div class="mai-task-understanding__section" data-testid="task-understanding-questions" role="group" aria-label="Open questions">
			<h4 class="mai-task-understanding__heading">Open questions</h4>
			<p class="mai-task-understanding__gap">
				No open questions recorded.
			</p>
		</div>

		<div class="mai-task-understanding__section" data-testid="task-understanding-plan" role="group" aria-label="Proposed plan">
			<h4 class="mai-task-understanding__heading">Proposed plan</h4>
			{#if steps.length === 0}
				<p class="mai-task-understanding__gap" data-testid="task-understanding-plan-empty">
					No plan steps recorded.
				</p>
			{:else}
				<ul class="mai-task-understanding__list" data-testid="task-understanding-step-list">
					{#each steps as step (step.step_id)}
						<li class="mai-task-understanding__row" data-testid="task-understanding-step-row">
							<span class="mai-task-understanding__row-key">
								{step.position}. {step.title}{#if sections.header.active_step_id === step.step_id}<span
										class="mai-task-understanding__active"> (active)</span
									>{/if}
							</span>
							<span class="mai-task-understanding__row-text">{stepDetail(step)}</span>
						</li>
					{/each}
				</ul>
				<p class="mai-task-understanding__meta" data-testid="task-understanding-plan-meta">
					Showing {steps.length} of {stepsTotal} steps.
				</p>
				{#if stepsHasMore}
					<Button
						variant="secondary"
						size="sm"
						loading={loadingMoreSteps}
						onclick={loadMoreSteps}
						data-testid="task-understanding-plan-more"
					>
						Load more steps
					</Button>
				{/if}
			{/if}
		</div>
	</section>
{:else}
	<section
		class="mai-task-understanding mai-task-understanding--flat"
		data-testid="task-understanding"
		data-state={uiState}
		aria-label="Task understanding"
	>
		{#if uiState === 'loading'}
			<p class="mai-task-understanding__gap" data-testid="task-understanding-loading">
				Loading task understanding…
			</p>
		{:else if uiState === 'unavailable'}
			<p class="mai-task-understanding__gap" data-testid="task-understanding-unavailable">
				Task understanding unavailable.
				<span class="mai-task-understanding__meta">
					No active task episode is recorded for this conversation in this server scope.
				</span>
			</p>
		{:else if uiState === 'error'}
			<p class="mai-task-understanding__gap" data-testid="task-understanding-error">
				Task understanding could not be loaded.
				<span class="mai-task-understanding__meta">
					<span data-testid="task-understanding-error-code">{problem?.code}</span>
					{problem?.message}
				</span>
			</p>
		{:else}
			<p class="mai-task-understanding__gap" data-testid="task-understanding-unbound">
				Task understanding unavailable.
				<span class="mai-task-understanding__meta">
					No task services are bound; nothing is fetched or fabricated.
				</span>
			</p>
		{/if}
	</section>
{/if}

<style>
	.mai-task-understanding {
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
	.mai-task-understanding--flat {
		padding: var(--mai-space-2, 8px);
	}
	.mai-task-understanding__scope {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 4px);
		min-width: 0;
	}
	.mai-task-understanding__title {
		font-size: var(--mai-text-sm, 0.875rem);
		font-weight: var(--mai-weight-semibold, 600);
		color: var(--mai-color-text, inherit);
		overflow-wrap: anywhere;
	}
	.mai-task-understanding__summary {
		font-size: var(--mai-text-xs, 0.75rem);
		color: var(--mai-color-text-muted, inherit);
		overflow-wrap: anywhere;
	}
	.mai-task-understanding__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 4px);
		min-width: 0;
	}
	.mai-task-understanding__heading {
		margin: 0;
		font-size: var(--mai-text-xs, 0.75rem);
		font-weight: var(--mai-weight-semibold, 600);
		color: var(--mai-color-text, inherit);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.mai-task-understanding__list {
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
	.mai-task-understanding__row {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: var(--mai-text-xs, 0.75rem);
		color: var(--mai-color-text-muted, inherit);
		overflow-wrap: anywhere;
		min-width: 0;
	}
	.mai-task-understanding__row-key {
		font-weight: var(--mai-weight-semibold, 600);
		color: var(--mai-color-text, inherit);
		overflow-wrap: anywhere;
	}
	.mai-task-understanding__row-text {
		overflow-wrap: anywhere;
	}
	.mai-task-understanding__origin,
	.mai-task-understanding__active {
		color: var(--mai-color-text-muted, inherit);
		font-style: italic;
	}
	.mai-task-understanding__gap {
		margin: 0;
		color: var(--mai-color-text, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
		overflow-wrap: anywhere;
	}
	.mai-task-understanding__meta {
		color: var(--mai-color-text-muted, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
		overflow-wrap: anywhere;
	}
</style>
