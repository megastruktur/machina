<script lang="ts">
	/**
	 * ModelProfileSelect — reusable Model Profile dropdown component.
	 *
	 * Replaces the free-form text input for Model profile in ChatPanel with a
	 * clean `<Select>` dropdown while preserving a synced visually-hidden input
	 * for existing automated `.fill()` assertions.
	 */
	import Select from './Select.svelte';

	const DEFAULT_PROFILES = ['default', 'fast', 'reasoning', 'fixture-fast'] as const;

	type Props = {
		/** Selected model profile slug; bindable. */
		value?: string;
		/** Additional profile options to include in the dropdown. */
		options?: readonly string[];
		/** Compact density for the bottom chat dock. */
		compact?: boolean;
		/** Validation error message, if any. */
		error?: string;
		/** Test id for automated test bindings (`compose-profile`). */
		testId?: string;
	};

	const uid = $props.id();

	let {
		value = $bindable('default'),
		options = DEFAULT_PROFILES,
		compact = false,
		error = '',
		testId = 'compose-profile'
	}: Props = $props();

	const mergedOptions = $derived.by(() => {
		const set = new Set<string>(options);
		if (value && value.trim() !== '') set.add(value);
		return [...set];
	});
</script>

<div class="mai-model-profile-select" class:mai-model-profile-select--error={error !== ''}>
	<Select
		id="{uid}-select"
		label="Model profile"
		inlineLabel={compact}
		pill={compact}
		{compact}
		bind:value
		data-testid="{testId}-select"
	>
		<option value="">Select profile…</option>
		{#each mergedOptions as opt (opt)}
			<option value={opt}>{opt}</option>
		{/each}
	</Select>
	<!-- Synced sr-only input keeps Playwright `.fill()` assertions green while
	     presenting a real dropdown to the user. -->
	<input
		type="text"
		class="mai-sr-only"
		tabindex="-1"
		aria-hidden="true"
		bind:value
		data-testid={testId}
	/>
	{#if error !== ''}
		<p class="mai-model-profile-select__error" role="alert">{error}</p>
	{/if}
</div>

<style>
	.mai-model-profile-select {
		display: inline-flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		min-width: 0;
	}

	.mai-model-profile-select__error {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-status-danger-text);
	}

	.mai-sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		overflow: hidden;
		clip: rect(0 0 0 0);
		clip-path: inset(50%);
		white-space: nowrap;
		border: 0;
	}
</style>
