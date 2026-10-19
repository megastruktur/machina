<script lang="ts">
	/**
	 * Shared labeled field primitive (card 01-ui-controls, P01; Odyssey UI upgrade).
	 *
	 * Layer: shared UI primitive (windowed-workspace ADR-0001 §2). Owns nothing:
	 * no window geometry, no drag, no server scoping, no stores, no context, no
	 * module-level `window`/`document` access (ADR §5 rule 4). Safe to render
	 * on the server (`$props.id()` works during SSR).
	 */
	import type { HTMLInputAttributes } from 'svelte/elements';

	type FieldType = 'text' | 'password' | 'email' | 'url' | 'search' | 'tel';

	type Props = Omit<HTMLInputAttributes, 'type' | 'value'> & {
		/** Visible label text, rendered in a <label> wired to the input. */
		label: string;
		/** Field kind; `password` renders an opaque input. */
		type?: FieldType;
		/** Current value; two-way bindable. */
		value?: string;
		/** Validation error message; shown (and announced) only when non-empty. */
		error?: string;
		/** Optional help text shown under the input. */
		hint?: string;
		/** Compact density for floating HUD/dock bars (hides verbose hint until focused/error). */
		compact?: boolean;
		/** Input element id; auto-generated per instance when omitted. */
		id?: string;
	};

	const generatedId = $props.id();

	let {
		label,
		type = 'text',
		value = $bindable(''),
		error = '',
		hint = '',
		compact = false,
		id = generatedId,
		class: className = '',
		...rest
	}: Props = $props();

	const hintId = $derived(`${id}-hint`);
	const errorId = $derived(`${id}-error`);

	const describedBy = $derived(
		[hint !== '' ? hintId : '', error !== '' ? errorId : ''].filter(Boolean).join(' ') || undefined
	);
</script>

<div
	class="mai-field"
	class:mai-field--error={error !== ''}
	class:mai-field--compact={compact}
>
	<!-- svelte-ignore a11y_label_has_associated_control -->
	<label class="mai-field__label" for={id}>{label}</label>
	<input
		{...rest}
		{id}
		{type}
		bind:value
		class="mai-field__input {className}"
		aria-invalid={error !== '' ? 'true' : undefined}
		aria-describedby={describedBy}
	/>
	{#if hint !== ''}
		<p class="mai-field__hint" class:mai-field__hint--compact={compact} id={hintId}>{hint}</p>
	{/if}
	{#if error !== ''}
		<p class="mai-field__error" id={errorId} role="alert">{error}</p>
	{/if}
</div>

<style>
	.mai-field {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1);
		min-width: 0;
	}

	.mai-field--compact {
		flex-direction: row;
		align-items: center;
		gap: var(--mai-space-2);
	}

	.mai-field__label {
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		color: var(--mai-color-text-muted);
		white-space: nowrap;
	}

	.mai-field__input {
		width: 100%;
		min-width: 0;
		min-height: var(--mai-target-min);
		padding: var(--mai-control-pad-y) var(--mai-control-pad-x);
		background: var(--mai-surface-glass-raised, var(--mai-color-surface));
		color: var(--mai-color-text);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		font-size: var(--mai-text-base);
		caret-color: var(--mai-color-brass);
		transition:
			border-color var(--mai-motion-fast) ease,
			background-color var(--mai-motion-fast) ease;
	}

	.mai-field--compact .mai-field__input {
		padding-top: var(--mai-space-1);
		padding-bottom: var(--mai-space-1);
		font-size: var(--mai-text-xs);
	}

	.mai-field__input:hover:not(:disabled) {
		border-color: var(--mai-color-border-strong);
		background: var(--mai-color-surface-raised);
	}

	.mai-field__input:focus {
		border-color: var(--mai-color-brass);
	}

	.mai-field__input:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	.mai-field__input[aria-invalid='true'] {
		border-color: var(--mai-status-danger-text);
	}

	.mai-field__hint {
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}

	.mai-field__hint--compact {
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

	.mai-field__error {
		font-size: var(--mai-text-xs);
		color: var(--mai-status-danger-text);
	}
</style>
