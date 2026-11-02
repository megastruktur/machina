<script module lang="ts">
	/**
	 * ObjectiveTimeline — the complete expandable activity timeline
	 * (runtime-v2.md §5/§8; rv_activity-ui AC2).
	 *
	 * One concise live narrative line (the latest ledger fact) plus the
	 * COMPLETE objective ledger as individually expandable rows: every
	 * `objective.*` event with its sanitized payload, and the work journal's
	 * checklist/actions/receipts/unknowns/children/verification sections
	 * with honest truncation markers. Payloads are the server-projected
	 * documents (redaction applied at WRITE) — this view never re-bounds
	 * silently and never claims access to private model chain-of-thought.
	 *
	 * Bounded rendering per AGENTS.md §1: the ledger renders its loaded
	 * page inside a scrollable region with an explicit "Load more" control
	 * (`hasMore`), and the journal's own drop-oldest truncation flags are
	 * surfaced verbatim. Details expand/collapse with native
	 * `<details>` (keyboard: Tab/Enter/Space; `aria-expanded` mirrors).
	 */
	import type { ObjectiveEventFrame, WorkJournal } from '../api/client.js';
</script>

<script lang="ts">
	let {
		events,
		total,
		hasMore,
		loadingMore = false,
		onloadmore,
		journal,
		error = null
	}: {
		/** The loaded ledger page (ascending sequence). */
		events: ObjectiveEventFrame[];
		/** Pre-slice matching count (same-filter total). */
		total: number;
		hasMore: boolean;
		loadingMore?: boolean;
		onloadmore: () => void;
		/** The derived work journal when one exists. */
		journal: WorkJournal | null;
		error?: string | null;
	} = $props();

	const EVENT_LABELS: Readonly<Record<string, string>> = Object.freeze({
		'objective.created': 'Objective created',
		'objective.scope_changed': 'Operator scope changed',
		'objective.status_changed': 'Objective status changed',
		'objective.journal_updated': 'Work journal updated',
		'objective.checklist_updated': 'Advisory checklist updated',
		'objective.completed': 'Objective completed'
	});

	function eventLabel(type: string): string {
		return EVENT_LABELS[type] ?? type;
	}

	function shortId(id: string | null): string {
		return id === null || id === '' ? '—' : id.slice(0, 8);
	}

	function formatTime(iso: string): string {
		const parsed = Date.parse(iso);
		if (Number.isNaN(parsed)) return iso;
		return new Date(parsed).toISOString().replace('T', ' ').slice(0, 19) + 'Z';
	}

	function payloadText(data: Record<string, unknown>): string {
		try {
			return JSON.stringify(data, null, 2);
		} catch {
			return String(data);
		}
	}

	const latest = $derived(events.length > 0 ? events[events.length - 1] : null);
	const narrative = $derived.by(() => {
		if (latest === null) return null;
		return `${eventLabel(latest.type)} (seq ${latest.sequence}, ${latest.actor_kind})`;
	});

	/** Journal sections render only when the journal exists; absence is the
	 * honest "not derived yet" state, never a fabricated empty document. */
	const checklist = $derived(journal?.checklist ?? null);
	const actions = $derived(journal?.actions ?? []);
	const receipts = $derived(journal?.receipts ?? []);
	const unknowns = $derived(journal?.unknowns ?? []);
	const children = $derived(journal?.children ?? []);
	const verification = $derived(journal?.verification ?? null);
</script>

<section
	class="acui-timeline"
	aria-label="Objective activity timeline"
	data-testid="objective-timeline"
>
	<div class="acui-timeline__narrative" role="status" data-testid="objective-narrative">
		{#if narrative !== null}
			Latest: {narrative}
		{:else if error === null}
			No objective activity is recorded yet.
		{/if}
	</div>

	{#if error !== null}
		<p class="acui-timeline__error" role="alert" data-testid="objective-timeline-error">
			{error}
		</p>
	{/if}

	<details class="acui-timeline__ledger" data-testid="objective-ledger">
		<summary>
			Complete activity ledger ({events.length} of {total} recorded events)
		</summary>
		<div class="acui-timeline__scroll" data-testid="objective-ledger-scroll">
			<ul class="acui-timeline__list">
				{#each events as event (event.sequence)}
					<li class="acui-timeline__item">
						<details class="acui-timeline__event" data-testid="objective-event" data-seq={event.sequence} data-type={event.type}>
							<summary data-testid="objective-event-summary">
								<span class="acui-timeline__seq">#{event.sequence}</span>
								<span class="acui-timeline__type">{eventLabel(event.type)}</span>
								<span class="acui-timeline__meta">{formatTime(event.ts)} · {event.actor_kind}</span>
								{#if event.run_id !== null}
									<span class="acui-timeline__meta">run {shortId(event.run_id)}</span>
								{/if}
							</summary>
							<pre class="acui-timeline__payload" data-testid="objective-event-payload">{payloadText(event.data)}</pre>
						</details>
					</li>
				{/each}
			</ul>
			{#if hasMore}
				<div class="acui-timeline__more">
					<button
						type="button"
						class="acui-timeline__more-btn"
						disabled={loadingMore}
						onclick={onloadmore}
						data-testid="objective-events-load-more"
					>
						{loadingMore ? 'Loading…' : `Load more events (${events.length} of ${total})`}
					</button>
				</div>
			{/if}
		</div>
	</details>

	<details class="acui-timeline__journal" data-testid="objective-journal">
		<summary>
			Work journal (receipts, verification, unknowns)
			{#if journal !== null && (journal.actions_truncated || journal.receipts_truncated || journal.unknowns_truncated || journal.children_truncated)}
				<span class="acui-timeline__truncated" data-testid="journal-truncated-marker">
					· older rows dropped at the bounded projection (truncated)
				</span>
			{/if}
		</summary>
		{#if journal === null}
			<p class="acui-timeline__meta" data-testid="journal-absent">
				The derived work journal is not available yet — it appears once the
				objective's ledger projects it.
			</p>
		{:else}
			<div class="acui-timeline__scroll">
				{#if checklist !== null}
					<details class="acui-timeline__section" data-testid="journal-checklist">
						<summary>
							Advisory checklist ({checklist.items?.length ?? 0})
							{#if checklist.malformed}
								<span class="acui-timeline__truncated" data-testid="journal-checklist-malformed">
									· last update was malformed (bookkeeping trouble, never evidence)
								</span>
							{/if}
						</summary>
						<ul class="acui-timeline__plain">
							{#each checklist.items ?? [] as item, itemIndex (itemIndex)}
								<li data-testid="journal-checklist-item">
									{item.done ? '☑' : '☐'} {item.text}
								</li>
							{/each}
						</ul>
					</details>
				{/if}
				<details class="acui-timeline__section" data-testid="journal-actions">
					<summary>
						Action attempts ({actions.length}{journal.actions_truncated ? '+, truncated' : ''})
					</summary>
					<ul class="acui-timeline__plain">
						{#each actions as action (action.action_id)}
							<li data-testid="journal-action">
								<span class="acui-timeline__type">{action.capability_id}</span>
								<span class="acui-timeline__meta">
									→ {shortId(action.server_id)} · decision {action.decision}
									{#if action.operation_id !== null && action.operation_id !== undefined}· op {shortId(action.operation_id)}{/if}
								</span>
								{#if action.detail !== null}
									<pre class="acui-timeline__payload">{action.detail}</pre>
								{/if}
							</li>
						{/each}
					</ul>
				</details>
				<details class="acui-timeline__section" data-testid="journal-receipts">
					<summary>
						Receipts ({receipts.length}{journal.receipts_truncated ? '+, truncated' : ''})
					</summary>
					<ul class="acui-timeline__plain">
						{#each receipts as receipt (receipt.operation_id)}
							<li data-testid="journal-receipt">
								op {shortId(receipt.operation_id)} · {receipt.outcome} · effect {receipt.effect_class} · target {shortId(receipt.server_id)}
							</li>
						{/each}
					</ul>
				</details>
				<details class="acui-timeline__section" data-testid="journal-unknowns">
					<summary>
						Unknown effects ({unknowns.length}{journal.unknowns_truncated ? '+, truncated' : ''})
					</summary>
					{#if unknowns.length === 0}
						<p class="acui-timeline__meta">No unknown effects are recorded.</p>
					{:else}
						<ul class="acui-timeline__plain">
							{#each unknowns as unknown (unknown.effect_ref)}
								<li data-testid="journal-unknown">
									{unknown.effect_ref} · {unknown.reconciled ? 'reconciled' : 'OPEN — classification required'}
									{#if unknown.server_id !== null && unknown.server_id !== undefined}· target {shortId(unknown.server_id)}{/if}
									{#if unknown.detail !== null}
										<pre class="acui-timeline__payload">{unknown.detail}</pre>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</details>
				<details class="acui-timeline__section" data-testid="journal-children">
					<summary>Delegation links ({children.length}{journal.children_truncated ? '+, truncated' : ''})</summary>
					{#if children.length === 0}
						<p class="acui-timeline__meta">No delegated children are recorded.</p>
					{:else}
						<ul class="acui-timeline__plain">
							{#each children as child (child.run_id)}
								<li data-testid="journal-child">
									run {shortId(child.run_id)} · servers {child.server_ids.map((one) => shortId(one)).join(', ')}
									{#if child.budget !== null && child.budget !== undefined}
										· bounded budget
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</details>
				<details class="acui-timeline__section" data-testid="journal-verification">
					<summary>Verification</summary>
					{#if verification === null}
						<p class="acui-timeline__meta" data-testid="journal-verification-absent">
							No verification verdict is recorded yet — completion is
							evidence-bound; model prose never completes an objective.
						</p>
					{:else}
						<div data-testid="journal-verification-verdict">
							<p class="acui-timeline__meta">
								task outcome {verification.verdict.task_outcome} · domain {verification.verdict.domain_outcome} ·
								certainty {verification.verdict.effect_certainty} · execution {verification.verdict.execution_status}
							</p>
							{#if verification.missing_evidence}
								<p class="acui-timeline__truncated" data-testid="journal-verification-missing-evidence">
									Evidence discipline downgraded this claim: no passed criterion
									bound to this run's succeeded receipts (completion_missing_evidence).
								</p>
							{/if}
							<ul class="acui-timeline__plain">
								{#each verification.verdict.criterion_results ?? [] as criterion (criterion.criterion_id)}
									<li data-testid="journal-criterion">
										{criterion.criterion_id}: {criterion.result}
										({(criterion.evidence_refs ?? []).length} evidence refs)
									</li>
								{/each}
							</ul>
						</div>
					{/if}
				</details>
			</div>
		{/if}
	</details>
</section>

<style>
	.acui-timeline {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		min-width: 0;
	}
	.acui-timeline__narrative {
		font-size: 0.75rem;
		color: var(--muted, inherit);
	}
	.acui-timeline__error {
		margin: 0;
		font-size: 0.75rem;
		color: var(--error, #c44);
	}
	.acui-timeline details {
		border: 1px solid var(--border, #4444);
		border-radius: 0.4rem;
	}
	.acui-timeline summary {
		cursor: pointer;
		padding: 0.25rem 0.5rem;
		font-size: 0.75rem;
		user-select: none;
	}
	.acui-timeline__scroll {
		max-height: 16rem;
		overflow-y: auto;
		padding: 0.25rem 0.5rem 0.4rem;
	}
	.acui-timeline__list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.acui-timeline__event summary {
		padding: 0.2rem 0.35rem;
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem 0.6rem;
		align-items: baseline;
	}
	.acui-timeline__seq {
		font-family: var(--font-mono, monospace);
		font-size: 0.68rem;
		color: var(--muted, inherit);
	}
	.acui-timeline__type {
		font-size: 0.72rem;
		font-weight: 600;
		overflow-wrap: anywhere;
	}
	.acui-timeline__meta {
		font-size: 0.68rem;
		color: var(--muted, inherit);
		overflow-wrap: anywhere;
	}
	.acui-timeline__payload {
		margin: 0.25rem 0 0.15rem;
		padding: 0.3rem;
		border-radius: 0.3rem;
		background: var(--surface, #1111);
		font-family: var(--font-mono, monospace);
		font-size: 0.66rem;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		max-height: 8rem;
		overflow-y: auto;
	}
	.acui-timeline__more {
		display: flex;
		justify-content: center;
		padding: 0.3rem 0 0.1rem;
	}
	.acui-timeline__more-btn {
		font: inherit;
		font-size: 0.72rem;
		padding: 0.15rem 0.6rem;
		border-radius: 999px;
		border: 1px solid var(--border, #4444);
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.acui-timeline__more-btn:disabled {
		cursor: default;
		color: var(--muted, inherit);
	}
	.acui-timeline__more-btn:focus-visible {
		outline: 2px solid var(--accent, #888);
		outline-offset: 1px;
	}
	.acui-timeline__truncated {
		color: var(--warning, #c90);
		font-size: 0.68rem;
	}
	.acui-timeline__section {
		margin-top: 0.25rem;
	}
	.acui-timeline__plain {
		margin: 0.2rem 0 0.3rem;
		padding-left: 1.1rem;
		font-size: 0.72rem;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
</style>
