<script lang="ts">
	/**
	 * SearchFilterInput — reusable search/filter input primitive.
	 *
	 * Provides a standardized search box with leading search glyph, optional
	 * match count badge (`shown / total`), Escape-to-clear keyboard handling,
	 * and a one-click clear (`×`) button. Designed for reuse across Inventory,
	 * Discovery, Documents, Jobs, and any other filterable list/grid in Machina.
	 */
	import type { HTMLInputAttributes } from 'svelte/elements';

	type Props = Omit<HTMLInputAttributes, 'value' | 'type'> & {
		/** Current search query string; bindable. */
		value?: string;
		/** Accessible label for the search input. */
		label?: string;
		/** Visually hide the label while keeping it accessible to screen readers. */
		hideLabel?: boolean;
		/** Optional filtered item count to display when a query is active. */
		resultCount?: number;
		/** Optional total item count to display alongside `resultCount`. */
		totalCount?: number;
		/** Base `data-testid` for the input (`{testId}-clear` is used for the clear button). */
		testId?: string;
	};

	const uid = $props.id();

	let {
		value = $bindable(''),
		label = 'Search filter',
		hideLabel = true,
		placeholder = 'Search or filter…',
		resultCount,
		totalCount,
		testId = 'search-filter-input',
		id = uid,
		class: className = '',
		...rest
	}: Props = $props();

	const hasQuery = $derived(value.trim() !== '');

	function handleKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape' && value !== '') {
			event.stopPropagation();
			value = '';
		}
	}
</script>

<div class="mai-search-filter {className}">
	<label class="mai-search-filter__label" class:mai-sr-only={hideLabel} for={id}>
		{label}
	</label>
	<div class="mai-search-filter__box" class:mai-search-filter__box--active={hasQuery}>
		<span class="mai-search-filter__icon" aria-hidden="true">⌕</span>
		<input
			{...rest}
			{id}
			type="search"
			class="mai-search-filter__input"
			{placeholder}
			bind:value
			onkeydown={handleKeydown}
			data-testid={testId}
		/>
		{#if resultCount !== undefined && totalCount !== undefined}
			<span class="mai-search-filter__count" data-testid="{testId}-count">
				{resultCount} / {totalCount}
			</span>
		{/if}
		{#if hasQuery}
			<button
				type="button"
				class="mai-search-filter__clear"
				aria-label="Clear filter"
				data-testid="{testId}-clear"
				onclick={() => (value = '')}
			>
				×
			</button>
		{/if}
	</div>
</div>

<style>
	.mai-search-filter {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		min-width: 14rem;
		flex: 1 1 16rem;
	}

	.mai-search-filter__label {
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		color: var(--mai-color-text-muted);
	}

	.mai-search-filter__box {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		min-height: 2.15rem;
		padding: 2px var(--mai-control-pad-x, var(--mai-space-3));
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-pill);
		transition:
			border-color var(--mai-motion-fast) ease,
			box-shadow var(--mai-motion-fast) ease;
	}

	.mai-search-filter__box:focus-within,
	.mai-search-filter__box--active {
		border-color: var(--mai-color-brass);
	}

	.mai-search-filter__icon {
		flex: 0 0 auto;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-brass);
		line-height: 1;
		user-select: none;
	}

	.mai-search-filter__input {
		flex: 1 1 auto;
		min-width: 0;
		padding: var(--mai-space-1) 0;
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text);
		background: transparent;
		border: none;
		outline: none;
	}

	.mai-search-filter__input::placeholder {
		color: var(--mai-color-text-muted);
	}

	/* Hide native WebKit search cancel button since we render our own themed × control. */
	.mai-search-filter__input::-webkit-search-cancel-button {
		-webkit-appearance: none;
		appearance: none;
	}

	.mai-search-filter__count {
		flex: 0 0 auto;
		padding: 1px var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: 0.7rem;
		color: var(--mai-color-text-muted);
		background: var(--mai-color-bg);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-pill);
	}

	.mai-search-filter__clear {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 1.35rem;
		height: 1.35rem;
		padding: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
		line-height: 1;
		color: var(--mai-color-text-muted);
		background: var(--mai-color-bg);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-pill);
		cursor: pointer;
	}

	.mai-search-filter__clear:hover {
		color: var(--mai-color-text);
		border-color: var(--mai-color-brass);
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
