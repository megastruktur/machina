<script lang="ts">
	/**
	 * CapabilityPicker — reusable combined dropdown + removable chips selector
	 * for server capability slugs (`host.shell`, `host.logs`, `app.install`, …).
	 *
	 * - Displays active capabilities as removable `<Chip>` badges (sorted alphabetically).
	 * - Provides a `<Select>` dropdown of all built-in Machina capabilities not yet selected
	 *   (picking one immediately adds it to `value`).
	 * - Also allows adding a custom plugin capability slug (`[a-z0-9][a-z0-9._-]*`).
	 * - Token-only styling (`--mai-*`).
	 */
	import Button from './Button.svelte';
	import Chip from './Chip.svelte';
	import Select from './Select.svelte';

	export interface CapabilityOption {
		id: string;
		description: string;
	}

	export const BUILTIN_CAPABILITIES: readonly CapabilityOption[] = [
		{ id: 'app.install', description: 'Install applications' },
		{ id: 'app.remove', description: 'Remove applications (keep data)' },
		{ id: 'app.remove-data', description: 'Remove applications & purge data' },
		{ id: 'app.update', description: 'Update applications' },
		{ id: 'docs.retrieve', description: 'Retrieve knowledge documents' },
		{ id: 'host.logs', description: 'Read systemd & container logs' },
		{ id: 'host.shell', description: 'Execute SSH shell commands' },
		{ id: 'memory.recall', description: 'Recall server memory' },
		{ id: 'memory.reflect', description: 'Reflect over server memory' },
		{ id: 'memory.retain', description: 'Retain facts in server memory' },
		{ id: 'model.install', description: 'Install & pull local models' },
		{ id: 'service.http', description: 'HTTP/REST service requests' },
		{ id: 'web.read', description: 'Read web pages' },
		{ id: 'web.search', description: 'Search the web' }
	];

	interface Props {
		/** Selected capability slugs. */
		value?: string[];
		/** Accessible label for the picker group. */
		label?: string;
		/** Optional helper text below the picker. */
		hint?: string;
		/** Disable interaction while saving. */
		disabled?: boolean;
		/** Test id prefix (defaults to `capability-picker`). */
		testId?: string;
	}

	let {
		value = $bindable<string[]>([]),
		label = 'Capabilities',
		hint = '',
		disabled = false,
		testId = 'capability-picker'
	}: Props = $props();

	const uid = $props.id();
	const SLUG_RE = /^[a-z0-9][a-z0-9._-]{0,127}$/;

	let selectedOption = $state('');
	let customSlug = $state('');
	let customError = $state('');

	/** Selected capabilities kept in alphabetical order. */
	const sortedSelected = $derived(
		[...value].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }))
	);

	/** Available built-in capabilities not yet in `value`, sorted alphabetically. */
	const availableOptions = $derived(
		BUILTIN_CAPABILITIES.filter((cap) => !value.includes(cap.id)).sort((a, b) =>
			a.id.localeCompare(b.id, undefined, { numeric: true, sensitivity: 'base' })
		)
	);

	function addCapability(slug: string): void {
		const trimmed = slug.trim().toLowerCase();
		if (!trimmed) return;
		if (!SLUG_RE.test(trimmed)) {
			customError = 'Capability slug must match [a-z0-9][a-z0-9._-]*';
			return;
		}
		customError = '';
		if (!value.includes(trimmed)) {
			value = [...value, trimmed].sort((a, b) =>
				a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })
			);
		}
	}

	function removeCapability(slug: string): void {
		if (disabled) return;
		value = value.filter((item) => item !== slug);
	}

	function addAllBuiltins(): void {
		if (disabled) return;
		const merged = new Set([...value, ...BUILTIN_CAPABILITIES.map((c) => c.id)]);
		value = [...merged].sort((a, b) =>
			a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })
		);
	}

	function onSelectChange(event: Event): void {
		const select = event.currentTarget as HTMLSelectElement;
		const chosen = select.value;
		selectedOption = '';
		select.value = '';
		if (chosen === '__all__') {
			addAllBuiltins();
		} else if (chosen) {
			addCapability(chosen);
		}
	}

	function onCustomKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter') {
			event.preventDefault();
			if (customSlug.trim()) {
				addCapability(customSlug);
				if (!customError) customSlug = '';
			}
		}
	}

	function onAddCustomClick(): void {
		if (customSlug.trim()) {
			addCapability(customSlug);
			if (!customError) customSlug = '';
		}
	}
</script>

<div class="mai-cap-picker" data-testid={testId}>
	<div class="mai-cap-picker__header">
		<label class="mai-cap-picker__label" for="mai-cap-select-{uid}">{label}</label>
		<span class="mai-cap-picker__count" data-testid="{testId}-count">
			{sortedSelected.length} selected
		</span>
	</div>

	<div class="mai-cap-picker__chips" data-testid="{testId}-chips" aria-label="Selected capabilities">
		{#if sortedSelected.length === 0}
			<span class="mai-cap-picker__empty" data-testid="{testId}-empty">
				No capabilities granted — pick from the dropdown below.
			</span>
		{:else}
			{#each sortedSelected as cap (cap)}
				<Chip
					variant="brass"
					removeLabel="Remove capability {cap}"
					removeTestId="{testId}-remove-{cap}"
					onremove={disabled ? undefined : () => removeCapability(cap)}
					data-testid="{testId}-chip"
				>
					{cap}
				</Chip>
			{/each}
		{/if}
	</div>

	<div class="mai-cap-picker__controls">
		<Select
			id="mai-cap-select-{uid}"
			bind:value={selectedOption}
			onchange={onSelectChange}
			{disabled}
			aria-label="Add capability"
			compact
			data-testid="{testId}-select"
		>
			<option value="">+ Add capability…</option>
			{#if availableOptions.length > 1}
				<option value="__all__">+ Add all built-in capabilities ({availableOptions.length})</option>
			{/if}
			{#each availableOptions as opt (opt.id)}
				<option value={opt.id}>{opt.id} — {opt.description}</option>
			{/each}
		</Select>

		<div class="mai-cap-picker__custom">
			<input
				type="text"
				class="mai-cap-picker__custom-input"
				placeholder="Custom capability slug (e.g. site.deploy)"
				bind:value={customSlug}
				onkeydown={onCustomKeydown}
				{disabled}
				aria-label="Custom capability slug"
				data-testid="{testId}-custom-input"
			/>
			<Button
				size="sm"
				variant="secondary"
				onclick={onAddCustomClick}
				disabled={disabled || !customSlug.trim()}
				data-testid="{testId}-custom-add"
			>
				Add
			</Button>
		</div>
	</div>

	{#if customError}
		<p class="mai-cap-picker__error" role="alert" data-testid="{testId}-error">{customError}</p>
	{:else if hint}
		<p class="mai-cap-picker__hint">{hint}</p>
	{/if}
</div>

<style>
	.mai-cap-picker {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}

	.mai-cap-picker__header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--mai-space-2);
	}

	.mai-cap-picker__label {
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-semibold);
		color: var(--mai-color-text-muted);
	}

	.mai-cap-picker__count {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-subtle);
	}

	.mai-cap-picker__chips {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--mai-space-1);
		min-height: 2.25rem;
		padding: var(--mai-space-2);
		background: var(--mai-color-surface-sunken);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
	}

	.mai-cap-picker__empty {
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-subtle);
		font-style: italic;
	}

	.mai-cap-picker__controls {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
		gap: var(--mai-space-2);
		align-items: center;
	}

	.mai-cap-picker__custom {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
	}

	.mai-cap-picker__custom-input {
		flex: 1 1 auto;
		min-width: 0;
		height: 2.125rem;
		padding: 0 var(--mai-space-3);
		color: var(--mai-color-text);
		background: var(--mai-color-surface-sunken);
		border: var(--mai-border-width) solid var(--mai-color-border-strong);
		border-radius: var(--mai-radius-md);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
	}

	.mai-cap-picker__custom-input:focus-visible {
		outline: var(--mai-focus-ring-width) solid var(--mai-focus-ring-color);
		outline-offset: var(--mai-focus-ring-offset);
		border-color: var(--mai-color-accent);
	}

	.mai-cap-picker__hint {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-subtle);
	}

	.mai-cap-picker__error {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-status-danger-text);
	}
</style>
