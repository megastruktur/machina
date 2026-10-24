<script lang="ts">
	/**
	 * TaskEvidence — value-free task outcome presentation (act `21-outcome-ui`;
	 * act_CONTRACTS §8/§9).
	 *
	 * Renders the additive `task_outcome` run-summary document produced by the
	 * final-response seam (`mai.agents.task_completion.OutcomeObservation.document()`):
	 * the derived domain outcome, the criteria counts, and every criterion's
	 * explicit state. Deliberately value-free like the wire document: only
	 * identifiers and frozen-vocabulary states are rendered — never criterion
	 * statements, model prose or payload material (C05 rule 4).
	 *
	 * Honesty rules (C08 rule 6; scenario E1):
	 * - The verdict is driven by the DOCUMENT, never by the run's technical
	 *   success state. "Fully verified" is rendered ONLY when the document
	 *   carries `verified === true` AND every listed criterion state is
	 *   `passed` — a successful run with unavailable/partial verification can
	 *   never render it.
	 * - `failed`/`unavailable` criteria stay visible as the outstanding work
	 *   (P3: the episode remains open and resumable).
	 * - The criterion list is bounded (AGENTS.md §1): a fixed page inside a
	 *   `overflow-y: auto` container, a `total` count and a "Load more"
	 *   control whenever more remain — never an unbounded dump.
	 * - `null` document renders nothing: a taskless run keeps every legacy
	 *   surface byte-identical.
	 *
	 * Svelte 5 runes only; no module-level state; tokens only (ADR §5 rule 4).
	 */
	import Button from '../../ui/Button.svelte';

	/** One frozen-vocabulary criterion state (wire: criterion_id + state only). */
	export type TaskOutcomeCriterion = {
		criterion_id: string;
		state: string;
	};

	/**
	 * The value-free `task_outcome` run-summary document, narrowed fail-closed
	 * by the panel from the wire. Shape mirrors
	 * `mai.agents.task_completion.OutcomeObservation.document()`.
	 */
	export type TaskOutcomeDocument = {
		task_id: string | null;
		outcome: string | null;
		verified: boolean;
		recorded: boolean;
		criteria: { total: number; supported: number; failed: number; unavailable: number };
		criterion_states: TaskOutcomeCriterion[];
		proposal: string;
		disposition?: string;
		revision?: number;
		rejection_code?: string;
		rejection_fields?: Record<string, string>;
	};

	let { document, pageSize = 10 }: { document: TaskOutcomeDocument | null; pageSize?: number } =
		$props();

	/** Bounded criterion page size (AGENTS.md §1). */
	const PAGE_LIMIT = $derived(pageSize > 0 ? pageSize : 10);

	/** Page window over the criterion list; resets when a new document lands. */
	let shown = $state(10);
	$effect(() => {
		void document;
		shown = PAGE_LIMIT;
	});

	/** "Fully verified" ONLY with the recorded flag AND an all-supported set. */
	const fullyVerified = $derived(
		document !== null &&
			document.verified === true &&
			document.criterion_states.length > 0 &&
			document.criterion_states.every((criterion) => criterion.state === 'passed')
	);

	const VERDICT_TEXT: Record<string, string> = {
		verified: 'verified',
		partially_verified: 'partially verified',
		in_progress: 'in progress',
		needs_input: 'needs input',
		failed: 'failed',
		cancelled: 'cancelled'
	};

	const verdict = $derived.by<string | null>(() => {
		if (document === null) return null;
		if (fullyVerified) return 'fully verified';
		if (document.outcome === null) return 'outcome not derived';
		return VERDICT_TEXT[document.outcome] ?? document.outcome;
	});

	/** Criteria still owed evidence: everything not explicitly supported. */
	const outstanding = $derived(
		document === null
			? []
			: document.criterion_states.filter((criterion) => criterion.state !== 'passed')
	);

	const visibleCriteria = $derived(
		document === null ? [] : document.criterion_states.slice(0, shown)
	);
	const hasMoreCriteria = $derived(
		document !== null && shown < document.criterion_states.length
	);

	function loadMoreCriteria(): void {
		shown = Math.min(shown + PAGE_LIMIT, document?.criterion_states.length ?? shown);
	}

	function criterionStateText(state: string): string {
		if (state === 'passed') return 'supported';
		return state;
	}
</script>

{#if document !== null}
	<section class="mai-task-evidence" data-testid="task-outcome" aria-label="Task outcome">
		<div class="mai-task-evidence__row">
			<span
				class="mai-task-evidence__verdict"
				class:mai-task-evidence__verdict--ok={fullyVerified}
				data-testid="task-outcome-verdict"
			>
				{verdict}
			</span>
			<span class="mai-task-evidence__counts" data-testid="task-outcome-counts">
				{document.criteria.supported} supported · {document.criteria.failed} failed ·
				{document.criteria.unavailable} unavailable · {document.criteria.total} total
			</span>
		</div>
		{#if outstanding.length > 0}
			<p class="mai-task-evidence__missing" data-testid="task-outcome-missing">
				Missing checks: {outstanding.length} of {document.criterion_states.length} criterion(s)
				outstanding — the task stays open and resumable.
			</p>
		{/if}
		<ul class="mai-task-evidence__criteria" data-testid="task-outcome-criteria">
			{#each visibleCriteria as criterion (criterion.criterion_id)}
				<li
					class="mai-task-evidence__criterion"
					class:mai-task-evidence__criterion--open={criterion.state !== 'passed'}
					data-testid="task-outcome-criterion"
				>
					<span class="mai-task-evidence__criterion-id">Criterion {criterion.criterion_id}</span>
					<span class="mai-task-evidence__criterion-state" data-state={criterion.state}>
						{criterionStateText(criterion.state)}
					</span>
				</li>
			{/each}
		</ul>
		<p class="mai-task-evidence__meta" data-testid="task-outcome-meta">
			Showing {visibleCriteria.length} of {document.criterion_states.length} criteria.
		</p>
		{#if hasMoreCriteria}
			<Button
				size="sm"
				variant="secondary"
				onclick={loadMoreCriteria}
				data-testid="task-outcome-load-more"
			>
				Load more criteria ({visibleCriteria.length} of {document.criterion_states.length})
			</Button>
		{/if}
		<p class="mai-task-evidence__proposal" data-testid="task-outcome-proposal">
			{#if document.proposal === 'accepted'}
				Completion recorded (revision {document.revision ?? '—'}).
			{:else if document.proposal === 'rejected'}
				Completion proposal rejected ({document.rejection_code ?? 'rejected'})
				{#if document.rejection_fields !== undefined && Object.keys(document.rejection_fields).length > 0}
					<span class="mai-task-evidence__fields" data-testid="task-outcome-rejection-fields">
						{Object.entries(document.rejection_fields)
							.map(([field, code]) => `${field}: ${code}`)
							.join(' · ')}
					</span>
				{/if}
			{:else}
				No completion proposal recorded — the outcome observation records nothing.
			{/if}
		</p>
	</section>
{/if}

<style>
	.mai-task-evidence {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 8px);
		margin: var(--mai-space-2, 8px) 0;
		padding: var(--mai-space-2, 8px);
		border: 1px solid var(--border-subtle, transparent);
		border-radius: var(--mai-radius-sm, 4px);
		background: var(--mai-color-surface-raised, transparent);
		max-width: 100%;
	}
	.mai-task-evidence__row {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: var(--mai-space-2, 8px);
	}
	.mai-task-evidence__verdict {
		font-size: var(--mai-text-sm, 0.875rem);
		font-weight: var(--mai-weight-semibold, 600);
		color: var(--mai-color-text, inherit);
	}
	.mai-task-evidence__verdict--ok {
		color: var(--mai-color-brass, inherit);
	}
	.mai-task-evidence__counts,
	.mai-task-evidence__meta {
		color: var(--mai-color-text-muted, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
	}
	.mai-task-evidence__missing {
		margin: 0;
		color: var(--mai-color-text, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
	}
	.mai-task-evidence__criteria {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 4px);
		max-height: 16rem;
		overflow-y: auto;
	}
	.mai-task-evidence__criterion {
		display: flex;
		justify-content: space-between;
		gap: var(--mai-space-2, 8px);
		font-size: var(--mai-text-xs, 0.75rem);
		color: var(--mai-color-text-muted, inherit);
		overflow-wrap: anywhere;
	}
	.mai-task-evidence__criterion--open .mai-task-evidence__criterion-state::after {
		content: ' · outstanding';
	}
	.mai-task-evidence__criterion-state {
		font-family: var(--mai-font-mono, monospace);
		flex: 0 0 auto;
	}
	.mai-task-evidence__proposal {
		margin: 0;
		color: var(--mai-color-text-muted, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
		overflow-wrap: anywhere;
	}
	.mai-task-evidence__fields {
		display: block;
		font-family: var(--mai-font-mono, monospace);
	}
</style>
