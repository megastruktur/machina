<script lang="ts">
	/**
	 * Shared labeled field primitive (card 01-ui-controls, P01).
	 *
	 * Layer: shared UI primitive (windowed-workspace ADR-0001 §2). Owns nothing:
	 * no window geometry, no drag, no server scoping, no stores, no context, no
	 * module-level `window`/`document` access (ADR §5 rule 4). Safe to render
	 * on the server (`$props.id()` works during SSR).
	 *
	 * Accessibility: a native <input> with a real <label for>; the visible
	 * keyboard focus ring comes from the global `:focus-visible` baseline in
	 * ui/tokens.css (ADR §6). We do not re-implement native behavior.
	 * A non-empty `error` sets `aria-invalid` on the input and links it via
	 * `aria-describedby` to the visible error text (`role="alert"`), so the
	 * error is announced and never color-only (C08 rule 6). `hint` is linked
	 * the same way.
	 *
	 * Secret non-disclosure (brief acceptance 2, windowed ADR §8 rule 1): the
	 * value lives only in the input element binding. It is never rendered as
	 * text, copied into any attribute, stored, or logged by this component.
	 *
	 * This is deliberately NOT a form generator: one labeled input + optional
	 * hint/error wiring, nothing more.
	 *
	 * Tokens come only from ui/tokens.css; no dependency is added (ADR §4.3).
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
		id = generatedId,
		class: className = '',
		...rest
	}: Props = $props();

	const hintId = $derived(`${id}-hint`);
	const errorId = $derived(`${id}-error`);

	// Reactive: the describedby list follows hint/error as they appear.
	const describedBy = $derived(
		[hint !== '' ? hintId : '', error !== '' ? errorId : ''].filter(Boolean).join(' ') || undefined
	);
</script>

<div class="mai-field" class:mai-field--error={error !== ''}>
	<!-- svelte-ignore a11y_label_has_associated_control -->
	<label class="mai-field__label" for={id}>{label}</label>
	<input
		{...rest}
		id={id}
		{type}
		bind:value
		class="mai-field__input {className}"
		aria-invalid={error !== '' ? 'true' : undefined}
		aria-describedby={describedBy}
	/>
	{#if hint !== ''}
		<p class="mai-field__hint" id={hintId}>{hint}</p>
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
	}

	.mai-field__label {
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-medium);
		color: var(--mai-color-text);
	}

	.mai-field__input {
		width: 100%;
		min-width: 0;
		min-height: var(--mai-target-min);
		padding: var(--mai-space-2) var(--mai-space-3);
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		font-size: var(--mai-text-base);
		transition: border-color var(--mai-motion-fast) ease;
	}

	.mai-field__input:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	.mai-field__input[aria-invalid='true'] {
		border-color: var(--mai-status-danger-text);
	}

	.mai-field__hint {
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
	}

	.mai-field__error {
		font-size: var(--mai-text-sm);
		color: var(--mai-status-danger-text);
	}
</style>
