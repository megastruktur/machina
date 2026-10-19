<script lang="ts">
	/**
	 * Select — shared reusable select-box primitive (Odyssey spatial UI).
	 *
	 * Layer: shared UI primitive (windowed-workspace ADR-0001 §2). Owns nothing:
	 * no window geometry, no drag, no server scoping, no stores, no module-level
	 * `window`/`document` access (ADR §5 rule 4). Safe to render during SSR.
	 *
	 * Renders a native `<select>` wrapped in a solid themed shell with an
	 * optional inline or stacked label, custom SVG chevron, helper hint, and
	 * validation error message so every dropdown across the HUD, Chat Dock,
	 * and feature panels shares one source of truth for colors, padding
	 * (`--mai-control-pad-*`), border, focus ring, and typography.
	 */
	import type { Snippet } from 'svelte';
	import type { HTMLSelectAttributes } from 'svelte/elements';

	type Props = Omit<HTMLSelectAttributes, 'value'> & {
		/** Current selected value; two-way bindable. */
		value?: string;
		/** Optional visible label text wired to the select element. */
		label?: string;
		/** Optional helper text rendered under the select. */
		hint?: string;
		/** Optional validation error message rendered under the select. */
		error?: string;
		/** Render label inline beside the select (for HUD/toolbar bars). */
		inlineLabel?: boolean;
		/** Visually hide the label while keeping it accessible to screen readers. */
		hideLabel?: boolean;
		/** Pill shape (rounded-full) for floating HUD / dock controls. */
		pill?: boolean;
		/** Compact vertical density for toolbars and chat composer strips. */
		compact?: boolean;
		/** Select element id; auto-generated per instance when omitted. */
		id?: string;
		/** `<option>` children. */
		children?: Snippet;
	};

	const generatedId = $props.id();

	let {
		value = $bindable(''),
		label = '',
		hint = '',
		error = '',
		inlineLabel = false,
		hideLabel = false,
		pill = false,
		compact = false,
		id = generatedId,
		class: className = '',
		children,
		...rest
	}: Props = $props();
</script>

<div
	class="mai-select-field"
	class:mai-select-field--inline={inlineLabel}
	class:mai-select-field--compact={compact}
>
	{#if label !== ''}
		<label
			class="mai-select-field__label"
			class:mai-select-field__label--sr={hideLabel}
			for={id}
		>
			{label}
		</label>
	{/if}
	<div class="mai-select-field__wrap" class:mai-select-field__wrap--pill={pill}>
		<select
			{...rest}
			{id}
			bind:value
			aria-invalid={error !== '' ? 'true' : undefined}
			class="mai-select-field__control {className}"
			class:mai-select-field__control--pill={pill}
			class:mai-select-field__control--compact={compact}
			class:mai-select-field__control--error={error !== ''}
		>
			{@render children?.()}
		</select>
		<svg class="mai-select-field__chevron" viewBox="0 0 12 12" aria-hidden="true">
			<path d="M3 4.5L6 7.5L9 4.5" />
		</svg>
	</div>
	{#if error !== ''}
		<p class="mai-select-field__error" role="alert">{error}</p>
	{:else if hint !== ''}
		<p class="mai-select-field__hint">{hint}</p>
	{/if}
</div>

<style>
	.mai-select-field {
		display: inline-flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		min-width: 0;
	}

	.mai-select-field--inline {
		flex-direction: row;
		align-items: center;
		gap: var(--mai-space-2);
	}

	.mai-select-field__label {
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		color: var(--mai-color-text-muted);
		white-space: nowrap;
	}

	.mai-select-field__label--sr {
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

	.mai-select-field__wrap {
		position: relative;
		display: inline-flex;
		align-items: center;
		min-width: 0;
		width: 100%;
	}

	.mai-select-field__control {
		appearance: none;
		width: 100%;
		min-height: var(--mai-target-min);
		padding: var(--mai-control-pad-y) calc(var(--mai-control-pad-x) + 18px)
			var(--mai-control-pad-y) var(--mai-control-pad-x);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-medium);
		color: var(--mai-color-text);
		background-color: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		cursor: pointer;
		transition:
			border-color var(--mai-motion-fast) ease,
			background-color var(--mai-motion-fast) ease;
	}

	.mai-select-field__control :global(option),
	.mai-select-field__control :global(optgroup) {
		color: var(--mai-color-text);
		background-color: var(--mai-color-surface-raised);
		font-weight: var(--mai-weight-medium);
	}

	.mai-select-field__control--compact {
		min-height: 2rem;
		padding-top: var(--mai-space-1);
		padding-bottom: var(--mai-space-1);
		font-size: var(--mai-text-xs);
	}

	.mai-select-field__control--pill {
		border-radius: var(--mai-radius-pill);
	}

	.mai-select-field__control--error {
		border-color: var(--mai-status-danger-border);
	}

	.mai-select-field__control:hover:not(:disabled) {
		border-color: var(--mai-color-brass);
		background-color: var(--mai-color-surface);
	}

	.mai-select-field__control:focus {
		border-color: var(--mai-color-brass);
	}

	.mai-select-field__chevron {
		position: absolute;
		right: var(--mai-space-2);
		width: 12px;
		height: 12px;
		pointer-events: none;
		fill: none;
		stroke: var(--mai-color-text-muted);
		stroke-width: 1.6;
		stroke-linecap: round;
		stroke-linejoin: round;
	}

	.mai-select-field__hint {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-select-field__error {
		margin: 0;
		font-size: var(--mai-text-xs);
		color: var(--mai-status-danger-text);
	}
</style>
