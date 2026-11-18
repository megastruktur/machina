<script lang="ts">
	/**
	 * Bounded learned operational notes (ask_31, D7/C3/C7) — Svelte 5 runes.
	 *
	 * Renders the ACTIVE learned-fact page, the append-only fact revision
	 * history and the durable preservation-operation receipts of ONE
	 * (server, application) scope — STRICTLY SEPARATE from any editable
	 * operator notes (those live in the Service Map surface; this component
	 * owns no editable notes and never writes operator state).
	 *
	 * Honesty (C7): every section carries its own loading/empty/error/ready
	 * state; conflict refusals (stale CAS, non-runnable retry) render as a
	 * bounded inline conflict note; nothing is invented or cached across
	 * scopes. Scope-epoch guard: when `serverId`/`applicationId` changes the
	 * epoch increments and every in-flight response from a previous scope
	 * is discarded (stale async answers can never paint a foreign scope).
	 *
	 * Bounded rendering: each section scrolls inside an explicit
	 * max-height box; collections page with PAGE_LIMIT and deduplicate
	 * appended rows by their durable identity.
	 *
	 * Accessibility: real buttons with descriptive accessible names, a
	 * polite status region, per-section headings bound via aria-labelledby,
	 * and full keyboard operability (Load more / Retract / Retry are
	 * focusable buttons; the optional reason input is a labeled field).
	 *
	 * Inert text: every learned statement/ref is rendered as escaped text
	 * interpolation — never {@html}. No credential or secret-shaped value is
	 * ever displayed (the domain model carries none).
	 */
		import { untrack } from 'svelte';
	import type {
		FactRevisionPage,
		KnowledgeApi,
		KnowledgeOperationStatus,
		KnowledgeRead,
		OperationPage,
		RetractBody
	} from '../workspace/entries/08-web-apps.js';

	let {
		serverId,
		applicationId,
		knowledge,
		pageLimit = 10,
		reasonMaxLength = 400
	}: {
		serverId: string;
		applicationId: string;
		/** The OPTIONAL knowledge capability group; `null` fails closed. */
		knowledge: KnowledgeApi | null | undefined;
		/** Bounded page size for every collection read (C1). */
		pageLimit?: number;
		/** Optional bounded operator reason for a retraction. */
		reasonMaxLength?: number;
	} = $props();

	interface FactRow {
		fact_id: string;
		revision: number;
		statement: string;
		status: string;
		source_kind: string;
		source_ref: string;
	}
	interface HistoryRow {
		revision_id: string;
		fact_id: string;
		revision: number;
		statement: string;
		status: string;
	}
	interface OperationRow {
		operation_id: string;
		status: KnowledgeOperationStatus;
		workflow_id: string;
		accepted_at: string;
	}
	type SectionState = 'loading' | 'ready' | 'empty' | 'error';

	let epoch = 0;

	let factsState = $state<SectionState>('loading');
	let factsError = $state('');
	let factItems = $state<FactRow[]>([]);
	let factsTotal = $state(0);
	let factsHasMore = $state(false);
	let factsOffset = $state(0);

	let historyState = $state<SectionState>('loading');
	let historyError = $state('');
	let historyItems = $state<HistoryRow[]>([]);
	let historyTotal = $state(0);
	let historyHasMore = $state(false);
	let historyOffset = $state(0);

	let operationsState = $state<SectionState>('loading');
	let operationsError = $state('');
	let operationItems = $state<OperationRow[]>([]);
	let operationsTotal = $state(0);
	let operationsHasMore = $state(false);
	let operationsOffset = $state(0);

	let generation = $state(0);
	let conflict = $state('');
	let busyFact = $state('');
	let busyOperation = $state('');
	let retractReason = $state('');

	const scopeKey = $derived(`${serverId}|${applicationId}`);

	function dedupeById<T extends { key: string }>(existing: T[], incoming: T[]): T[] {
		const known = new Set(existing.map((row) => row.key));
		return [...existing, ...incoming.filter((row) => !known.has(row.key))];
	}

	function errorMessage(error: unknown): string {
		if (error instanceof Error && 'code' in error) {
			const code = String((error as { code: unknown }).code);
			return code === '' ? 'request failed' : code;
		}
		return 'request failed';
	}

	async function loadFacts(currentEpoch: number, offset: number, append: boolean) {
		if (knowledge === null || knowledge === undefined) return;
		factsState = 'loading';
		try {
			const page = await knowledge.readKnowledge(serverId, applicationId, {
				limit: pageLimit,
				offset
			});
			if (currentEpoch !== epoch) return; // stale scope response: discarded
			const rows: FactRow[] = page.facts.items.map((fact) => ({
				fact_id: fact.fact_id,
				revision: fact.revision,
				statement: fact.statement,
				status: fact.status,
				source_kind: fact.source.source_kind,
				source_ref: fact.source.source_ref
			}));
			factItems = append
				? dedupeById(
						factItems.map((row) => ({ key: row.fact_id, ...row })),
						rows.map((row) => ({ key: row.fact_id, ...row }))
					).map(({ key: _key, ...rest }) => rest)
				: rows;
			factsTotal = page.facts.total;
			generation = page.generation;
			factsHasMore = page.facts.has_more;
			factsOffset = offset + rows.length;
			factsState = factItems.length === 0 ? 'empty' : 'ready';
		} catch (error) {
			if (currentEpoch !== epoch) return;
			factsError = errorMessage(error);
			factsState = 'error';
		}
	}

	async function loadHistory(currentEpoch: number, offset: number, append: boolean) {
		if (knowledge === null || knowledge === undefined) return;
		historyState = 'loading';
		try {
			const page: FactRevisionPage = await knowledge.readFactHistory(
				serverId,
				applicationId,
				undefined,
				{ limit: pageLimit, offset }
			);
			if (currentEpoch !== epoch) return;
			const rows: HistoryRow[] = page.items.map((row) => ({
				revision_id: row.revision_id,
				fact_id: row.fact_id,
				revision: row.revision,
				statement: row.statement,
				status: row.status
			}));
			historyItems = append
				? dedupeById(
						historyItems.map((r) => ({ key: r.revision_id, ...r })),
						rows.map((r) => ({ key: r.revision_id, ...r }))
					).map(({ key: _key, ...rest }) => rest)
				: rows;
			historyTotal = page.total;
			historyHasMore = page.has_more;
			historyOffset = offset + rows.length;
			historyState = historyItems.length === 0 ? 'empty' : 'ready';
		} catch (error) {
			if (currentEpoch !== epoch) return;
			historyError = errorMessage(error);
			historyState = 'error';
		}
	}

	async function loadOperations(currentEpoch: number, offset: number, append: boolean) {
		if (knowledge === null || knowledge === undefined) return;
		operationsState = 'loading';
		try {
			const page: OperationPage = await knowledge.readKnowledgeOperations(
				serverId,
				applicationId,
				undefined,
				{ limit: pageLimit, offset }
			);
			if (currentEpoch !== epoch) return;
			const rows: OperationRow[] = page.items.map((row) => ({
				operation_id: row.operation_id,
				status: row.status as KnowledgeOperationStatus,
				workflow_id: row.workflow_id,
				accepted_at: row.accepted_at
			}));
			operationItems = append
				? dedupeById(
						operationItems.map((row) => ({ key: row.operation_id, ...row })),
						rows.map((r) => ({ key: r.operation_id, ...r }))
					).map(({ key: _key, ...rest }) => rest)
				: rows;
			operationsTotal = page.total;
			operationsHasMore = page.has_more;
			operationsOffset = offset + rows.length;
			operationsState = operationItems.length === 0 ? 'empty' : 'ready';
		} catch (error) {
			if (currentEpoch !== epoch) return;
			operationsError = errorMessage(error);
			operationsState = 'error';
		}
	}

	async function reload(currentEpoch: number) {
		conflict = '';
		busyFact = '';
		busyOperation = '';
		retractReason = '';
		await Promise.all([
			loadFacts(currentEpoch, 0, false),
			loadHistory(currentEpoch, 0, false),
			loadOperations(currentEpoch, 0, false)
		]);
	}

	// Scope-epoch guard: a server/application change invalidates every
	// in-flight response of the previous scope.
	$effect(() => {
		void scopeKey;
		untrack(() => {
			epoch += 1;
			void reload(epoch);
		});
	});

	function loadMoreFacts() {
		void loadFacts(epoch, factsOffset, true);
	}
	function loadMoreHistory() {
		void loadHistory(epoch, historyOffset, true);
	}
	function loadMoreOperations() {
		void loadOperations(epoch, operationsOffset, true);
	}
	async function reloadAll() {
		void reload(epoch);
	}

	async function retractFact(fact: FactRow) {
		if (knowledge === null || knowledge === undefined) return;
		const body: RetractBody = {
			expected_revision: fact.revision,
			expected_generation: generation,
			...(retractReason.trim() === ''
				? {}
				: { reason: retractReason.trim().slice(0, reasonMaxLength) })
		};
		busyFact = fact.fact_id;
		conflict = '';
		try {
			const result = await knowledge.retractKnowledgeFact(
				serverId,
				applicationId,
				fact.fact_id,
				body
			);
			if (result.status === 'applied') {
				await reload(epoch);
				return;
			}
			conflict = result.error_code ?? 'revision_conflict';
		} catch (error) {
			conflict = errorMessage(error);
		} finally {
			busyFact = '';
		}
	}

	async function retryOperation(operation: OperationRow) {
		if (knowledge === null || knowledge === undefined) return;
		busyOperation = operation.operation_id;
		conflict = '';
		try {
			await knowledge.retryKnowledgeOperation(
				serverId,
				applicationId,
				operation.operation_id
			);
			await reload(epoch);
		} catch (error) {
			conflict = errorMessage(error);
		} finally {
			busyOperation = '';
		}
	}

	const factsBusy = $derived(factsState === 'loading');
	const historyBusy = $derived(historyState === 'loading');
	const operationsBusy = $derived(operationsState === 'loading');
</script>

<section class="service-knowledge" data-testid="knowledge-root" aria-label="Learned operational notes">
	<p data-testid="knowledge-status" role="status" aria-live="polite" class="knowledge-status">
		{knowledge === null || knowledge === undefined
			? 'Knowledge surface unavailable'
			: factsState === 'loading' || historyState === 'loading' || operationsState === 'loading'
				? 'Loading learned notes'
				: conflict !== ''
					? `Conflict: ${conflict}`
					: factsState === 'error' || historyState === 'error' || operationsState === 'error'
						? 'A knowledge read failed'
						: 'Ready'}
	</p>

	{#if knowledge === null || knowledge === undefined}
		<p data-testid="knowledge-unavailable" class="knowledge-note">
			The knowledge capability group is not wired into this panel; learned
			notes are unavailable (nothing is faked).
		</p>
	{/if}

	{#if conflict !== ''}
		<p data-testid="knowledge-conflict" class="knowledge-conflict" role="alert">
			{conflict}
		</p>
	{/if}

	<label class="knowledge-reason-label" for="knowledge-reason">Retraction reason (optional)</label>
	<input
		id="knowledge-reason"
		data-testid="knowledge-reason"
		class="knowledge-reason"
		type="text"
		bind:value={retractReason}
		maxlength={reasonMaxLength}
		placeholder="optional bounded reason"
	/>

	<button
		type="button"
		class="knowledge-reload"
		data-testid="knowledge-reload"
		onclick={reloadAll}
	>
		Reload
	</button>

	<h3 id="knowledge-facts-heading" class="knowledge-heading">Learned facts</h3>
	<div class="knowledge-scroll" data-testid="knowledge-facts" role="region" aria-labelledby="knowledge-facts-heading">
		{#if factsState === 'loading'}
			<p class="knowledge-note">Loading facts…</p>
		{:else if factsState === 'empty'}
			<p class="knowledge-note" data-testid="knowledge-facts-empty">No learned facts yet.</p>
		{:else if factsState === 'error'}
			<p class="knowledge-note knowledge-error">Facts read failed: {factsError}</p>
		{:else}
			<ul class="knowledge-list">
				{#each factItems as fact (fact.fact_id)}
					<li class="knowledge-row" data-testid="knowledge-fact-row" data-fact-id={fact.fact_id}>
						<span class="knowledge-text">{fact.statement}</span>
						<span class="knowledge-meta">
							source: {fact.source_kind} · ref {fact.source_ref}
						</span>
						<button
							type="button"
							class="knowledge-retract"
							data-testid="knowledge-fact-retract"
							disabled={busyFact !== ''}
							aria-label={`Retract fact ${fact.fact_id} at revision ${fact.revision}`}
							onclick={() => void retractFact(fact)}
						>
							Retract
						</button>
					</li>
				{/each}
			</ul>
			{#if factsHasMore}
				<button
					type="button"
					class="knowledge-load-more"
					data-testid="knowledge-facts-load-more"
					disabled={factsBusy}
					onclick={loadMoreFacts}
				>
					Load more facts
				</button>
			{/if}
		{/if}
	</div>

	<h3 id="knowledge-history-heading" class="knowledge-heading">Fact history</h3>
	<div class="knowledge-scroll" data-testid="knowledge-history" role="region" aria-labelledby="knowledge-history-heading">
		{#if historyState === 'loading'}
			<p class="knowledge-note">Loading history…</p>
		{:else if historyState === 'empty'}
			<p class="knowledge-note" data-testid="knowledge-history-empty">No fact history yet.</p>
		{:else if historyState === 'error'}
			<p class="knowledge-note knowledge-error">History read failed: {historyError}</p>
		{:else}
			<ul class="knowledge-list">
				{#each historyItems as row (row.revision_id)}
					<li class="knowledge-row" data-testid="knowledge-history-row" data-revision-id={row.revision_id}>
						<span class="knowledge-text">{row.statement}</span>
						<span class="knowledge-meta">rev {row.revision} · {row.status}</span>
					</li>
				{/each}
			</ul>
			{#if historyHasMore}
				<button
					type="button"
					class="knowledge-load-more"
					data-testid="knowledge-history-load-more"
					disabled={historyBusy}
					onclick={loadMoreHistory}
				>
					Load more history
				</button>
			{/if}
		{/if}
	</div>

	<h3 id="knowledge-operations-heading" class="knowledge-heading">Preservation operations</h3>
	<div class="knowledge-scroll" data-testid="knowledge-operations" role="region" aria-labelledby="knowledge-operations-heading">
		{#if operationsState === 'loading'}
			<p class="knowledge-note">Loading operations…</p>
		{:else if operationsState === 'empty'}
			<p class="knowledge-note" data-testid="knowledge-operations-empty">No preservation operations yet.</p>
		{:else if operationsState === 'error'}
			<p class="knowledge-note knowledge-error">Operations read failed: {operationsError}</p>
		{:else}
			<ul class="knowledge-list">
				{#each operationItems as operation (operation.operation_id)}
					<li class="knowledge-row" data-testid="knowledge-operation-row" data-operation-id={operation.operation_id}>
						<span class="knowledge-text">{operation.status}</span>
						<span class="knowledge-meta">{operation.workflow_id}</span>
						{#if operation.status === 'queued'}
							<button
								type="button"
								class="knowledge-retry"
								data-testid="knowledge-operation-retry"
								disabled={busyOperation !== ''}
								aria-label={`Retry operation ${operation.operation_id}`}
								onclick={() => void retryOperation(operation)}
							>
								Retry
							</button>
						{/if}
					</li>
				{/each}
			</ul>
			{#if operationsHasMore}
				<button
					type="button"
					class="knowledge-load-more"
					data-testid="knowledge-operations-load-more"
					disabled={operationsBusy}
					onclick={loadMoreOperations}
				>
					Load more operations
				</button>
			{/if}
		{/if}
	</div>
</section>

<style>
	.service-knowledge {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 8px;
	}
	.knowledge-scroll {
		overflow-y: auto;
		max-height: 320px;
		border: 1px solid currentColor;
		padding: 4px;
	}
	.knowledge-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.knowledge-row {
		display: flex;
		flex-direction: column;
		gap: 2px;
		border-bottom: 1px dotted currentColor;
		padding-bottom: 4px;
	}
	.knowledge-heading {
		margin: 0;
		font-size: 1rem;
	}
	.knowledge-note,
	.knowledge-meta {
		opacity: 0.8;
		margin: 0;
	}
	.knowledge-error {
		font-weight: 600;
	}
	.knowledge-conflict {
		font-weight: 600;
		margin: 0;
	}
	.knowledge-status {
		margin: 0;
	}
	.knowledge-text {
		/* Inert text rendering: escaped interpolation only, no links. */
		user-select: text;
	}
	.knowledge-reason {
		max-width: 100%;
	}
</style>
