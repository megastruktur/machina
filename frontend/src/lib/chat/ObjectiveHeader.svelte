<script module lang="ts">
	/**
	 * ObjectiveHeader — the persistent v2 run header (runtime-v2.md §9 UI;
	 * rv_activity-ui AC1).
	 *
	 * Every field renders RECORDED truth only: the objective detail row
	 * (status/revision/scope/budget), the derived work journal (children
	 * links, open unknowns), the replayed run states and the live activity
	 * the panel folded (model, elapsed, current action). Nothing is guessed:
	 * an unrecorded field renders its honest absence marker (C08 rule 6).
	 *
	 * Wait-state derivation is the frozen §9 vocabulary, value-free:
	 * `waiting_approval` / `waiting_children` / `reconciling` / `paused`
	 * come from the replayed run state, `reconciling` also from open
	 * unknown effects, `waiting_operator` from pending steering rows.
	 * Tokens come only from ui/tokens.css. No module-level state (SSR safe).
	 */
	import type { ObjectiveDetail, WorkJournal } from '../api/client.js';
</script>

<script lang="ts">
	type RunLink = {
		run_id: string;
		parent_run_id?: string | null;
		initiating_server_id: string;
	};

	let {
		detail,
		journal,
		runs,
		runStates,
		runState = null,
		model = null,
		elapsedSeconds = null,
		currentAction = null,
		pendingSteering = 0,
		serverLabels
	}: {
		/** The scope-checked objective detail (server truth). */
		detail: ObjectiveDetail;
		/** The derived work journal when one exists (null = not yet derived). */
		journal: WorkJournal | null;
		/** The objective's run bindings (bounded page content). */
		runs: RunLink[];
		/** Replayed run state per run id, when already read. */
		runStates: Readonly<Record<string, string | null>>;
		/** The primary (root) run's replayed state, when read. */
		runState?: string | null;
		/** The bound model of the live/most recent run (panel fold). */
		model?: string | null;
		/** Live elapsed seconds of the active run (panel fold). */
		elapsedSeconds?: number | null;
		/** Concise current action (latest activity label; panel fold). */
		currentAction?: string | null;
		/** Pending steering rows (the operator's queued control input). */
		pendingSteering?: number;
		/** Display labels per server id. */
		serverLabels: Readonly<Record<string, string>>;
	} = $props();

	const RUN_STATE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		queued: 'Queued',
		running: 'Running',
		waiting_approval: 'Waiting for approval decision',
		waiting_children: 'Waiting for child runs',
		reconciling: 'Reconciling an unproven outcome',
		paused: 'Paused by operator',
		succeeded: 'Succeeded',
		failed: 'Failed',
		cancelled: 'Cancelled (verified)'
	});

	function shortId(id: string | null): string {
		return id === null || id === '' ? '—' : id.slice(0, 8);
	}

	function labelFor(serverId: string): string {
		return serverLabels[serverId] ?? `Server ${shortId(serverId)}`;
	}

	const openUnknowns = $derived((journal?.unknowns ?? []).filter((one) => !one.reconciled));

	/**
	 * The §9 wait vocabulary. Priority: explicit run state first, then the
	 * derived honest overrides (open unknowns ⇒ reconciling even before the
	 * run row replays the park; pending steering ⇒ the operator's control
	 * input is waiting).
	 */
	const waitState = $derived.by<string | null>(() => {
		if (runState === 'waiting_approval') return 'waiting_approval';
		if (runState === 'waiting_children') return 'waiting_children';
		if (runState === 'reconciling') return 'reconciling';
		if (runState === 'paused') return 'paused';
		if (openUnknowns.length > 0) return 'reconciling';
		if (pendingSteering > 0) return 'waiting_operator';
		if (runState === 'queued' || runState === 'running') return null;
		return null;
	});

	const waitText = $derived.by(() => {
		if (waitState === 'waiting_approval') return 'Waiting for approval decision';
		if (waitState === 'waiting_children') return 'Waiting for child runs';
		if (waitState === 'reconciling') return 'Unknown outcome — reconciliation required';
		if (waitState === 'paused') return 'Paused by operator';
		if (waitState === 'waiting_operator') return 'Waiting for the operator (steering queued)';
		return null;
	});

	const childRuns = $derived(runs.filter((one) => one.parent_run_id !== null && one.parent_run_id !== undefined));
	const childStates = $derived(
		childRuns
			.map((one) => runStates[one.run_id] ?? null)
			.filter((state): state is string => state !== null)
	);
	const childrenActive = $derived(
		childStates.filter((state) => state !== 'succeeded' && state !== 'failed' && state !== 'cancelled')
			.length
	);

	const budgetText = $derived.by(() => {
		const budget = detail.budget;
		if (budget === null || budget === undefined) return 'unbounded';
		const parts: string[] = [];
		if (budget.max_active_runs !== null && budget.max_active_runs !== undefined)
			parts.push(`runs ≤ ${budget.max_active_runs}`);
		if (budget.max_total_steps !== null && budget.max_total_steps !== undefined)
			parts.push(`steps ≤ ${budget.max_total_steps}`);
		if (budget.max_total_tokens !== null && budget.max_total_tokens !== undefined)
			parts.push(`tokens ≤ ${budget.max_total_tokens}`);
		return parts.length === 0 ? 'unbounded' : parts.join(', ');
	});

	const statusText = $derived.by(() => {
		if (runState !== null && runState !== undefined && runState !== '') {
			const base = RUN_STATE_TEXT[runState] ?? `Recorded state: ${runState}`;
			if (
				(runState === 'running' || runState === 'queued') &&
				currentAction !== null &&
				currentAction !== ''
			) {
				return `${base} — ${currentAction}`;
			}
			return base;
		}
		if (detail.status === 'completed') return 'Completed';
		if (detail.status === 'failed') return 'Failed';
		if (detail.status === 'cancelled') return 'Cancelled';
		if (currentAction !== null && currentAction !== '') return currentAction;
		return 'Active — no run activity recorded yet';
	});
</script>

<section
	class="acui-header"
	aria-label="Objective run header"
	data-testid="objective-header"
>
	<div class="acui-header__row">
		<span
			class="acui-header__status"
			class:acui-header__status--live={runState === 'running' || runState === 'queued'}
			class:acui-header__status--ok={runState === 'succeeded' || detail.status === 'completed'}
			class:acui-header__status--danger={
				runState === 'failed' || detail.status === 'failed'
			}
			class:acui-header__status--neutral={
				runState === 'cancelled' || detail.status === 'cancelled'
			}
			class:acui-header__status--warn={waitState !== null}
			data-testid="objective-status"
			role="status"
		>
			{statusText}
		</span>
		<span class="acui-header__title" data-testid="objective-title">
			{detail.title && detail.title.trim() !== '' ? detail.title : `Objective ${shortId(detail.objective_id)}`}
		</span>
		<span class="acui-header__meta" data-testid="objective-target">
			Target {detail.scope.map((one) => labelFor(one.server_id)).join(', ')}
		</span>
		<span class="acui-header__meta" data-testid="objective-model">
			Model {model ?? 'not recorded'}
		</span>
		{#if elapsedSeconds !== null && (runState === 'running' || runState === 'queued')}
			<span class="acui-header__meta" data-testid="objective-elapsed">{elapsedSeconds}s elapsed</span>
		{/if}
		{#if waitText !== null}
			<span class="acui-header__wait" data-testid="objective-wait" role="status">{waitText}</span>
		{/if}
	</div>
	<div class="acui-header__row acui-header__row--meta">
		<span class="acui-header__meta" data-testid="objective-budget">Budget: {budgetText}</span>
		<span class="acui-header__meta" data-testid="objective-children">
			{#if childRuns.length === 0}
				No delegated children
			{:else}
				Children: {childRuns.length}{childrenActive > 0 ? ` · ${childrenActive} active` : ''}
			{/if}
		</span>
		{#if openUnknowns.length > 0}
			<span class="acui-header__unknown" data-testid="objective-unknowns" role="status">
				{openUnknowns.length} unknown effect{openUnknowns.length === 1 ? '' : 's'} — classification required
			</span>
		{/if}
		<span class="acui-header__meta" data-testid="objective-revision">Revision {detail.revision}</span>
	</div>
</section>

<style>
	.acui-header {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.5rem 0.6rem;
		border: 1px solid var(--border, #4444);
		border-radius: 0.5rem;
		background: var(--surface, transparent);
	}
	.acui-header__row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.4rem 0.75rem;
		min-width: 0;
	}
	.acui-header__row--meta {
		gap: 0.4rem 0.9rem;
	}
	.acui-header__status {
		display: inline-flex;
		align-items: center;
		padding: 0.05rem 0.45rem;
		border-radius: 999px;
		border: 1px solid var(--border, #4444);
		font-size: 0.72rem;
		letter-spacing: 0.02em;
		white-space: nowrap;
	}
	.acui-header__status--live {
		border-color: var(--accent, #888);
		color: var(--accent, inherit);
	}
	.acui-header__status--ok {
		border-color: var(--success, #4a4);
		color: var(--success, inherit);
	}
	.acui-header__status--danger {
		border-color: var(--error, #c44);
		color: var(--error, inherit);
	}
	.acui-header__status--neutral {
		color: var(--muted, inherit);
	}
	.acui-header__status--warn {
		border-color: var(--warning, #c90);
		color: var(--warning, inherit);
	}
	.acui-header__title {
		font-weight: 600;
		font-size: 0.82rem;
		overflow-wrap: anywhere;
	}
	.acui-header__meta {
		font-size: 0.72rem;
		color: var(--muted, inherit);
		white-space: nowrap;
	}
	.acui-header__wait,
	.acui-header__unknown {
		font-size: 0.72rem;
		color: var(--warning, #c90);
		white-space: nowrap;
	}
	.acui-header__unknown {
		color: var(--error, #c44);
	}
</style>
