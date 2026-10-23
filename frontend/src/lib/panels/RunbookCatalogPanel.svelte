<script module lang="ts">
	/**
	 * RunbookCatalogPanel — the GLOBAL Runbook Catalog window (card
	 * `uir-u4-runbooks-apps`; `Global / Fleet`).
	 *
	 * Layer: reusable feature panel (windowed-workspace ADR §7.2). It owns
	 * NO window mechanics: everything enters through the single
	 * `panel: PanelContext` prop (validated scope + explicit services bag)
	 * and instances share nothing (NT5; SSR-safe — no module-level mutable
	 * state).
	 *
	 * The catalog is the server-agnostic half of the applications split:
	 * this window manages runbook RECIPES only (identity, category,
	 * description and the five operational instruction slots). Per-server
	 * state (`installed`, custom instructions) lives on each server's
	 * Applications window — deleting a catalog recipe never touches a
	 * server map entry.
	 *
	 * Wire honesty (C08 rule 6): loading/empty/unavailable/error/ready
	 * states are always rendered honestly; pagination is real (bounded
	 * page, offset/total/hasMore, deduplicated Load more) inside a bounded
	 * scrollable container; every mutation reports its problem verbatim.
	 *
	 * Hostile content: names/descriptions render as inert escaped text
	 * only — there is deliberately no `{@html}` in this file.
	 */
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import Button from '../ui/Button.svelte';
	import SearchFilterInput from '../ui/SearchFilterInput.svelte';
	import Select from '../ui/Select.svelte';
	import type { PanelContext } from '../workspace/registry.js';
	import {
		RUNBOOK_STEPS,
		readRunbookCatalogServices,
		type RunbookStep,
		type RunbookUpsertBody,
		type RunbookView,
		type ServiceCategoryView
	} from '../workspace/entries/13-web-runbooks.js';

	let { panel }: { panel: PanelContext } = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	const api = $derived(readRunbookCatalogServices(panel));
	/** The catalog is a fleet-wide surface: a server scope contradicts the entry. */
	const globalScope = $derived(panel.scope.kind === 'global');
	const usable = $derived(api !== null && globalScope);

	// --- producer bounds mirrored for display only (server re-validates) -----

	/** Bounded catalog page size (the route's default page window). */
	const PAGE_LIMIT = 10;
	/** Bounded runbook name (service_map.py MAX_NAME_CHARS). */
	const MAX_NAME_CHARS = 200;
	/** Bounded description (service_map.py MAX_DESCRIPTION_CHARS). */
	const MAX_DESCRIPTION_CHARS = 2000;
	/** Bounded per-slot instruction (service_map.py MAX_INSTRUCTION_CHARS). */
	const MAX_INSTRUCTION_CHARS = 8000;

	function emptyInstructions(): Record<RunbookStep, string> {
		return { install: '', update: '', uninstall: '', backup: '', verify: '' };
	}

	// --- catalog page (bounded, deduplicated Load more) -----------------------

	let runbooks = $state<RunbookView[]>([]);
	let total = $state(0);
	let loadingMore = $state(false);
	let listState = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
	let problem = $state<{ code: string; message: string } | null>(null);
	let epoch = 0;
	const hasMore = $derived(listState === 'ready' && runbooks.length < total);

	function codeOf(error: unknown): string | null {
		if (typeof error !== 'object' || error === null || !('code' in error)) return null;
		return typeof error.code === 'string' ? error.code : null;
	}

	function messageOf(error: unknown): string | null {
		return error instanceof Error && error.message ? error.message : null;
	}

	function friendlyText(code: string, fallback: string): string {
		if (code === 'unauthenticated') return 'Your session has ended — sign in again.';
		if (code === 'throttled') return 'Too many requests — try again shortly.';
		if (code === 'conflict') return 'The record changed elsewhere; refresh and reapply.';
		if (code === 'not_found') return 'Nothing is stored under this id.';
		if (code === 'unavailable')
			return 'The catalog service is unavailable right now; nothing was changed.';
		if (code === 'validation_failed') return 'The request was not accepted; see the details.';
		return fallback;
	}

	/** The merged taxonomy carried by the latest page (fallback: empty). */
	let categories = $state<ServiceCategoryView[]>([]);

	async function loadCatalog(): Promise<void> {
		if (api === null) return;
		const current = ++epoch;
		listState = 'loading';
		loadingMore = false;
		problem = null;
		try {
			const page = await api.listRunbooks({ limit: PAGE_LIMIT, offset: 0 });
			if (current !== epoch) return;
			runbooks = page.runbooks;
			total = page.total;
			categories = page.categories ?? [];
			listState = 'ready';
		} catch (error) {
			if (current !== epoch) return;
			const code = codeOf(error) ?? 'unavailable';
			runbooks = [];
			total = 0;
			listState = 'error';
			problem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The runbook catalog could not be read.')
			};
		}
	}

	async function loadMore(): Promise<void> {
		if (api === null || listState !== 'ready' || loadingMore || !hasMore) return;
		const current = epoch;
		loadingMore = true;
		problem = null;
		try {
			const page = await api.listRunbooks({
				limit: PAGE_LIMIT,
				offset: runbooks.length
			});
			if (current !== epoch) return;
			const seen = new Set(runbooks.map((r) => r.runbook_id));
			const appended = page.runbooks.filter((r) => !seen.has(r.runbook_id));
			runbooks = [...runbooks, ...appended];
			total = page.total;
		} catch (error) {
			if (current !== epoch) return;
			const code = codeOf(error) ?? 'unavailable';
			problem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'More runbooks could not be read.')
			};
		} finally {
			loadingMore = false;
		}
	}

	// --- category filter pills + search ---------------------------------------

	let categoryFilter = $state<string>('all');
	let searchQuery = $state<string>('');

	const presentCategories = $derived.by<string[]>(() => {
		const present = new Set(runbooks.map((r) => r.category));
		return categories.map((c) => c.machine_id).filter((id) => present.has(id));
	});

	function categoryLabel(machineId: string): string {
		return categories.find((c) => c.machine_id === machineId)?.label ?? machineId;
	}

	function recordedSteps(runbook: RunbookView): Array<[RunbookStep, string]> {
		const steps: Array<[RunbookStep, string]> = [];
		for (const step of RUNBOOK_STEPS) {
			const text = runbook.instructions?.[step];
			if (typeof text === 'string' && text.trim() !== '') steps.push([step, text]);
		}
		return steps;
	}

	const filteredRunbooks = $derived.by<RunbookView[]>(() => {
		const byCategory =
			categoryFilter === 'all'
				? runbooks
				: runbooks.filter((r) => r.category === categoryFilter);
		const q = searchQuery.trim().toLowerCase();
		if (q === '') return byCategory;
		return byCategory.filter((r) =>
			[r.name, r.description, r.category, categoryLabel(r.category), r.runbook_id].some(
				(part) => part.toLowerCase().includes(q)
			)
		);
	});

	// --- create / edit form (one form for both; textareas everywhere) ----------

	let formTarget = $state<{ runbook_id: string | null } | null>(null);
	let formName = $state('');
	let formCategory = $state('system');
	let formDescription = $state('');
	let formInstructions = $state<Record<RunbookStep, string>>(emptyInstructions());
	let formFields = $state<Record<string, string>>({});
	let formBusy = $state(false);
	let formProblem = $state<{ code: string; message: string } | null>(null);
	let formDone = $state<string | null>(null);

	function startCreate(): void {
		formTarget = { runbook_id: null };
		formName = '';
		formCategory = 'system';
		formDescription = '';
		formInstructions = emptyInstructions();
		formFields = {};
		formProblem = null;
		formDone = null;
	}

	function startEdit(runbook: RunbookView): void {
		formTarget = { runbook_id: runbook.runbook_id };
		formName = runbook.name;
		formCategory = runbook.category;
		formDescription = runbook.description ?? '';
		formInstructions = emptyInstructions();
		for (const step of RUNBOOK_STEPS) {
			const text = runbook.instructions?.[step];
			if (typeof text === 'string') formInstructions[step] = text;
		}
		formFields = {};
		formProblem = null;
		formDone = null;
	}

	function cancelForm(): void {
		formTarget = null;
		formFields = {};
		formProblem = null;
	}

	function validateForm(): Record<string, string> {
		const errors: Record<string, string> = {};
		const name = formName.trim();
		if (name === '') errors.name = 'A display name is required.';
		else if (name.length > MAX_NAME_CHARS)
			errors.name = `The name is bounded to ${MAX_NAME_CHARS} characters.`;
		if (formDescription.length > MAX_DESCRIPTION_CHARS)
			errors.description = `Bounded to ${MAX_DESCRIPTION_CHARS} characters.`;
		for (const step of RUNBOOK_STEPS) {
			if (formInstructions[step].length > MAX_INSTRUCTION_CHARS)
				errors[step] = `Bounded to ${MAX_INSTRUCTION_CHARS} characters.`;
		}
		return errors;
	}

	async function saveForm(): Promise<void> {
		if (api === null || formTarget === null || formBusy) return;
		const errors = validateForm();
		formFields = errors;
		if (Object.keys(errors).length > 0) return;
		const body: RunbookUpsertBody = {
			name: formName.trim(),
			category: formCategory,
			description: formDescription.trim(),
			instructions: Object.fromEntries(
				RUNBOOK_STEPS.map((step) => [step, formInstructions[step].trim()])
			) as Record<RunbookStep, string>
		};
		if (formTarget.runbook_id !== null) body.runbook_id = formTarget.runbook_id;
		formBusy = true;
		formProblem = null;
		try {
			const saved = await api.upsertRunbook(body);
			formTarget = null;
			formDone = saved.runbook_id;
			await loadCatalog();
		} catch (error) {
			const code = codeOf(error) ?? 'unavailable';
			formProblem = {
				code,
				message: friendlyText(code, messageOf(error) ?? 'The runbook could not be saved.')
			};
		} finally {
			formBusy = false;
		}
	}

	// --- delete (two-step confirm; catalog-scoped only) ------------------------

	let deleteTarget = $state<RunbookView | null>(null);
	let deleteBusy = $state(false);
	let deleteProblem = $state<{ code: string; message: string } | null>(null);

	function startDelete(runbook: RunbookView): void {
		deleteTarget = runbook;
		deleteProblem = null;
	}

	function cancelDelete(): void {
		deleteTarget = null;
		deleteProblem = null;
	}

	async function confirmDelete(): Promise<void> {
		const target = deleteTarget;
		if (api === null || target === null || deleteBusy) return;
		deleteBusy = true;
		deleteProblem = null;
		try {
			await api.deleteRunbook(target.runbook_id);
			deleteTarget = null;
			await loadCatalog();
		} catch (error) {
			const code = codeOf(error) ?? 'unavailable';
			deleteProblem = {
				code,
				message:
					code === 'not_found'
						? 'This runbook has no global catalog record (it is a server-seeded projection); nothing was deleted.'
						: friendlyText(code, messageOf(error) ?? 'The runbook could not be deleted.')
			};
		} finally {
			deleteBusy = false;
		}
	}

	// --- mount cycle (the $effect teardown is the release path) ---------------

	$effect(() => {
		if (!usable) return;
		void loadCatalog();
	});

	onDestroy(() => {
		epoch += 1;
	});
</script>

<div class="mai-catalog" data-testid="runbooks-root">
	{#if !usable}
		<div class="mai-catalog__state mai-catalog__state--error" role="alert" data-testid="runbooks-config-error">
			<p>
				This window needs its catalog service and a global scope. Server-specific
				install state lives on each server's Applications window.
			</p>
		</div>
	{:else}
		<header class="mai-catalog__header">
			<h3 class="mai-catalog__title">Runbook Catalog</h3>
			<p class="mai-catalog__meta">
				Global recipes for every fleet server — install procedures, not install state.
			</p>
			<div class="mai-catalog__actions">
				<Button variant="secondary" onclick={loadCatalog} data-testid="catalog-refresh">
					Refresh
				</Button>
				<Button onclick={startCreate} data-testid="catalog-new-runbook">+ New Runbook</Button>
			</div>
		</header>

		{#if listState === 'loading'}
			<div class="mai-catalog__state" role="status" data-testid="catalog-loading">
				<p>Loading the runbook catalog…</p>
			</div>
		{:else if listState === 'error'}
			<div
				class="mai-catalog__state mai-catalog__state--error"
				role="alert"
				data-testid="catalog-error"
				data-error-code={problem?.code}
			>
				<p>{problem?.message}</p>
				<Button variant="secondary" onclick={loadCatalog} data-testid="catalog-retry">
					Retry
				</Button>
			</div>
		{:else if runbooks.length === 0}
			<div class="mai-catalog__state" role="status" data-testid="catalog-empty">
				<p>No runbooks are catalogued yet. Create one with + New Runbook; services already
					mapped on a server appear here automatically.</p>
			</div>
		{:else}
			<div class="mai-catalog__toolbar">
				<p class="mai-catalog__meta" data-testid="catalog-count">
					Showing {runbooks.length} of {total} runbooks.
				</p>
				<SearchFilterInput
					bind:value={searchQuery}
					label="Search runbooks"
					placeholder="Filter by name, description, or category…"
					resultCount={filteredRunbooks.length}
					totalCount={runbooks.length}
					testId="catalog-search"
				/>
			</div>
			{#if presentCategories.length > 0}
				<div class="mai-catalog__filterbar" role="group" aria-label="Filter runbooks by category">
					<button
						type="button"
						class="mai-catalog__pill"
						class:mai-catalog__pill--active={categoryFilter === 'all'}
						aria-pressed={categoryFilter === 'all'}
						onclick={() => (categoryFilter = 'all')}
						data-testid="catalog-category-pill"
						data-category="all"
					>
						All
					</button>
					{#each presentCategories as machineId (machineId)}
						<button
							type="button"
							class="mai-catalog__pill"
							class:mai-catalog__pill--active={categoryFilter === machineId}
							aria-pressed={categoryFilter === machineId}
							onclick={() => (categoryFilter = machineId)}
							data-testid="catalog-category-pill"
							data-category={machineId}
						>
							{categoryLabel(machineId)}
						</button>
					{/each}
				</div>
			{/if}

			<div class="mai-catalog__scroll" data-testid="catalog-scroll">
				{#if filteredRunbooks.length === 0}
					<div class="mai-catalog__state" role="status" data-testid="catalog-filtered-empty">
						<p>No runbooks match this filter.</p>
					</div>
				{/if}
				<ul class="mai-catalog__rows" data-testid="catalog-list">
					{#each filteredRunbooks as runbook (runbook.runbook_id)}
						<li class="mai-catalog__row" data-testid="catalog-row" data-runbook-id={runbook.runbook_id}>
							<div class="mai-catalog__row-main">
								<span class="mai-catalog__label" data-testid="catalog-row-name">{runbook.name}</span>
								<span
									class="mai-catalog__chip"
									data-testid="catalog-row-category"
									data-category={runbook.category}
								>
									{categoryLabel(runbook.category)}
								</span>
								<span class="mai-catalog__meta" title={runbook.runbook_id}>
									r{runbook.revision} · {runbook.updated_at}
								</span>
							</div>
							{#if runbook.description}
								<p class="mai-catalog__description" data-testid="catalog-row-description">
									{runbook.description}
								</p>
							{/if}
							<details class="mai-catalog__runbook" data-testid="catalog-row-instructions">
								<summary>Operational runbook</summary>
								{#if recordedSteps(runbook).length === 0}
									<p class="mai-catalog__note">No runbook instructions recorded.</p>
								{:else}
									<dl class="mai-catalog__facts">
										{#each recordedSteps(runbook) as [step, text] (step)}
											<dt>{step}</dt>
											<dd data-testid="catalog-runbook-step" data-step={step}>{text}</dd>
										{/each}
									</dl>
								{/if}
							</details>
							{#if formTarget?.runbook_id === runbook.runbook_id}
								<form
									class="mai-catalog__form"
									data-testid="catalog-edit-form"
									onsubmit={(event) => {
										event.preventDefault();
										void saveForm();
									}}
								>
									<label class="mai-catalog__field">
										<span>Name</span>
										<input
											type="text"
											bind:value={formName}
											maxlength={MAX_NAME_CHARS}
											aria-invalid={formFields.name !== undefined ? 'true' : undefined}
											data-testid="catalog-edit-name"
										/>
									</label>
									{#if formFields.name}
										<p class="mai-catalog__error">{formFields.name}</p>
									{/if}
									<Select label="Category" bind:value={formCategory} data-testid="catalog-edit-category">
										{#each categories as entry (entry.machine_id)}
											<option value={entry.machine_id}>{entry.label}</option>
										{/each}
									</Select>
									<label class="mai-catalog__field">
										<span>Description</span>
										<textarea
											bind:value={formDescription}
											rows="3"
											maxlength={MAX_DESCRIPTION_CHARS}
											data-testid="catalog-edit-description"
										></textarea>
									</label>
									<fieldset class="mai-catalog__fieldset">
										<legend>Runbook instructions</legend>
										{#each RUNBOOK_STEPS as step (step)}
											<label class="mai-catalog__field">
												<span>{step}</span>
												<textarea
													bind:value={formInstructions[step]}
													rows="3"
													maxlength={MAX_INSTRUCTION_CHARS}
													data-testid="catalog-edit-instructions-{step}"
												></textarea>
											</label>
										{/each}
									</fieldset>
									<div class="mai-catalog__actions">
										<Button type="submit" loading={formBusy} data-testid="catalog-edit-save">
											Save runbook
										</Button>
										<Button variant="secondary" onclick={cancelForm} data-testid="catalog-edit-cancel">
											Cancel
										</Button>
									</div>
									{#if formProblem !== null}
										<div
											class="mai-catalog__state mai-catalog__state--error"
											role="alert"
											data-testid="catalog-edit-error"
											data-error-code={formProblem.code}
										>
											<p>{formProblem.message}</p>
										</div>
									{/if}
								</form>
							{/if}
							<div class="mai-catalog__row-actions">
								<Button
									variant="secondary"
									onclick={() => startEdit(runbook)}
									data-testid="catalog-edit-start"
								>
									✎ Edit
								</Button>
								<Button
									variant="danger"
									onclick={() => startDelete(runbook)}
									data-testid="catalog-delete-start"
								>
									Delete
								</Button>
							</div>
							{#if deleteTarget?.runbook_id === runbook.runbook_id}
								<div class="mai-catalog__confirm" data-testid="catalog-delete-confirm">
									<p>
										Delete the catalog recipe
										<strong data-testid="catalog-delete-name">{deleteTarget?.name}</strong>?
										Server map entries are never touched — removing the recipe never
										uninstalls or unmapped a service.
									</p>
									<div class="mai-catalog__actions">
										<Button variant="danger" loading={deleteBusy} onclick={confirmDelete} data-testid="catalog-delete-accept">
											Confirm delete
										</Button>
										<Button variant="secondary" onclick={cancelDelete} data-testid="catalog-delete-cancel">
											Cancel
										</Button>
									</div>
									{#if deleteProblem !== null}
										<div
											class="mai-catalog__state mai-catalog__state--error"
											role="alert"
											data-testid="catalog-delete-error"
											data-error-code={deleteProblem.code}
										>
											<p>{deleteProblem.message}</p>
										</div>
									{/if}
								</div>
							{/if}
						</li>
					{/each}
				</ul>
			</div>
			{#if hasMore}
				<div class="mai-catalog__actions">
					<Button
						variant="secondary"
						size="sm"
						loading={loadingMore}
						onclick={loadMore}
						data-testid="catalog-load-more"
					>
						Load more ({runbooks.length} of {total})
					</Button>
				</div>
			{/if}
			{#if problem !== null && listState === 'ready'}
				<div
					class="mai-catalog__state mai-catalog__state--error"
					role="alert"
					data-testid="catalog-page-error"
					data-error-code={problem.code}
				>
					<p>{problem.message}</p>
				</div>
			{/if}

			{#if formTarget?.runbook_id === null}
				<form
					class="mai-catalog__form"
					data-testid="catalog-create-form"
					onsubmit={(event) => {
						event.preventDefault();
						void saveForm();
					}}
				>
					<h4>New catalog runbook</h4>
					<label class="mai-catalog__field">
						<span>Name</span>
						<input
							type="text"
							bind:value={formName}
							maxlength={MAX_NAME_CHARS}
							aria-invalid={formFields.name !== undefined ? 'true' : undefined}
							data-testid="catalog-create-name"
						/>
					</label>
					{#if formFields.name}
						<p class="mai-catalog__error">{formFields.name}</p>
					{/if}
					<Select label="Category" bind:value={formCategory} data-testid="catalog-create-category">
						{#each categories as entry (entry.machine_id)}
							<option value={entry.machine_id}>{entry.label}</option>
						{/each}
					</Select>
					<label class="mai-catalog__field">
						<span>Description</span>
						<textarea
							bind:value={formDescription}
							rows="3"
							maxlength={MAX_DESCRIPTION_CHARS}
							data-testid="catalog-create-description"
						></textarea>
					</label>
					<fieldset class="mai-catalog__fieldset">
						<legend>Runbook instructions</legend>
						{#each RUNBOOK_STEPS as step (step)}
							<label class="mai-catalog__field">
								<span>{step}</span>
								<textarea
									bind:value={formInstructions[step]}
									rows="3"
									maxlength={MAX_INSTRUCTION_CHARS}
									data-testid="catalog-create-instructions-{step}"
								></textarea>
							</label>
						{/each}
					</fieldset>
					<div class="mai-catalog__actions">
						<Button type="submit" loading={formBusy} data-testid="catalog-create-save">
							Save runbook
						</Button>
						<Button variant="secondary" onclick={cancelForm} data-testid="catalog-create-cancel">
							Cancel
						</Button>
					</div>
					{#if formProblem !== null}
						<div
							class="mai-catalog__state mai-catalog__state--error"
							role="alert"
							data-testid="catalog-create-error"
							data-error-code={formProblem.code}
						>
							<p>{formProblem.message}</p>
						</div>
					{/if}
				</form>
			{/if}
			{#if formDone !== null}
				<p class="mai-catalog__note" role="status" data-testid="catalog-saved">
					Runbook saved (revision committed to the global catalog).
				</p>
			{/if}
		{/if}
	{/if}
</div>

<style>
	.mai-catalog {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3, 0.75rem);
		padding: var(--mai-panel-pad, 1rem);
		color: var(--mai-color-text, inherit);
		font-size: var(--mai-text-sm, 0.875rem);
		min-width: 0;
		min-height: 0;
		overflow: auto;
	}

	.mai-catalog__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-catalog__title {
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-base, 1rem);
		letter-spacing: 0.04em;
	}

	h4 {
		margin: 0;
		font-size: var(--mai-text-sm, 0.875rem);
	}

	.mai-catalog__meta {
		margin: 0;
		color: var(--mai-color-text-muted, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-catalog__actions {
		display: flex;
		gap: var(--mai-space-2, 0.5rem);
		align-items: center;
		flex-wrap: wrap;
	}

	.mai-catalog__toolbar {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-catalog__filterbar {
		display: flex;
		gap: var(--mai-space-1, 0.25rem);
		flex-wrap: wrap;
	}

	.mai-catalog__pill {
		border: 1px solid var(--mai-color-border, currentColor);
		border-radius: 999px;
		background: transparent;
		color: inherit;
		padding: 0.15rem 0.7rem;
		font-size: var(--mai-text-xs, 0.75rem);
		cursor: pointer;
	}

	.mai-catalog__pill--active {
		background: var(--mai-color-primary, currentColor);
		color: var(--mai-color-primary-contrast, inherit);
	}

	.mai-catalog__scroll {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		max-height: 26rem;
		overflow-y: auto;
		border: 1px solid var(--mai-color-border, currentColor);
		border-radius: 0.5rem;
		padding: var(--mai-space-2, 0.5rem);
	}

	.mai-catalog__rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
	}

	.mai-catalog__row {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		border: 1px solid var(--mai-color-border, currentColor);
		border-radius: 0.5rem;
		padding: var(--mai-space-2, 0.5rem);
	}

	.mai-catalog__row-main {
		display: flex;
		gap: var(--mai-space-2, 0.5rem);
		align-items: baseline;
		flex-wrap: wrap;
	}

	.mai-catalog__label {
		font-weight: var(--mai-weight-semibold, 600);
	}

	.mai-catalog__chip {
		border: 1px solid var(--mai-color-border, currentColor);
		border-radius: 999px;
		padding: 0 0.5rem;
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-catalog__description {
		margin: 0;
		white-space: pre-wrap;
	}

	.mai-catalog__facts {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.15rem var(--mai-space-2, 0.5rem);
		margin: 0;
	}

	.mai-catalog__facts dt {
		font-family: var(--mai-font-mono, monospace);
		font-size: var(--mai-text-xs, 0.75rem);
		color: var(--mai-color-text-muted, inherit);
	}

	.mai-catalog__facts dd {
		margin: 0;
		white-space: pre-wrap;
		font-family: var(--mai-font-mono, monospace);
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-catalog__row-actions {
		display: flex;
		gap: var(--mai-space-2, 0.5rem);
		flex-wrap: wrap;
	}

	.mai-catalog__form {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		border: 1px solid var(--mai-color-border, currentColor);
		border-radius: 0.5rem;
		padding: var(--mai-space-2, 0.5rem);
	}

	.mai-catalog__fieldset {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		border: 1px dashed var(--mai-color-border, currentColor);
		border-radius: 0.5rem;
		padding: var(--mai-space-2, 0.5rem);
		margin: 0;
	}

	.mai-catalog__field {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.mai-catalog__field span {
		font-size: var(--mai-text-xs, 0.75rem);
		color: var(--mai-color-text-muted, inherit);
	}

	.mai-catalog__field input,
	.mai-catalog__field textarea {
		font: inherit;
		color: inherit;
		background: transparent;
		border: 1px solid var(--mai-color-border, currentColor);
		border-radius: 0.35rem;
		padding: 0.3rem 0.5rem;
	}

	.mai-catalog__field textarea {
		resize: vertical;
		font-family: var(--mai-font-mono, monospace);
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-catalog__confirm {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		border: 1px solid var(--mai-color-warning, currentColor);
		border-radius: 0.5rem;
		padding: var(--mai-space-2, 0.5rem);
	}

	.mai-catalog__confirm p {
		margin: 0;
	}

	.mai-catalog__state {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 0.5rem);
		padding: var(--mai-space-2, 0.5rem);
		border-radius: 0.5rem;
	}

	.mai-catalog__state p {
		margin: 0;
	}

	.mai-catalog__state--error {
		border: 1px solid var(--mai-color-danger, currentColor);
	}

	.mai-catalog__note {
		margin: 0;
		color: var(--mai-color-text-muted, inherit);
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-catalog__error {
		margin: 0;
		color: var(--mai-color-danger, currentColor);
		font-size: var(--mai-text-xs, 0.75rem);
	}
</style>
