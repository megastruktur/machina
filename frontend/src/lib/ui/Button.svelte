<script lang="ts">
	/**
	 * Shared button primitive (card 01-ui-controls, P01).
	 *
	 * Layer: shared UI primitive (windowed-workspace ADR-0001 §2). Owns nothing:
	 * no window geometry, no drag, no server scoping, no stores, no module-level
	 * `window`/`document` access (ADR §5 rule 4). Safe to render on the server.
	 *
	 * Accessibility: a native <button> is used as-is — native Enter/Space
	 * activation, native disabled semantics, and the global `:focus-visible`
	 * focus ring from ui/tokens.css (ADR §6). We do not re-implement any of
	 * that ("reuse selected accessible primitives rather than duplicate
	 * library internals").
	 *
	 * Tokens: colors, spacing, radii, type and motion come only from
	 * ui/tokens.css custom properties (ADR §2 "backed by design tokens");
	 * this component redeclares no theme values and adds no dependency
	 * (ADR §4.3).
	 *
	 * `loading` implies disabled (aria-busy + disabled): a busy control must
	 * not accept further activation (C08 rule 6: never all-green on
	 * unavailable data).
	 */
	import type { Snippet } from 'svelte';
	import type { HTMLButtonAttributes } from 'svelte/elements';

	type Variant = 'primary' | 'secondary' | 'danger';

	type Props = HTMLButtonAttributes & {
		/** Visual variant; defaults to the primary action style. */
		variant?: Variant;
		/** Pending state: disables the button and sets aria-busy. */
		loading?: boolean;
		/** Button label content. */
		children?: Snippet;
	};

	let {
		variant = 'primary',
		loading = false,
		type = 'button',
		disabled = false,
		class: className = '',
		children,
		...rest
	}: Props = $props();
</script>

<button
	{type}
	disabled={disabled || loading}
	aria-busy={loading || undefined}
	class="mai-button mai-button--{variant} {className}"
	{...rest}
>
	{#if loading}
		<span class="mai-button__spinner" aria-hidden="true"></span>
	{/if}
	{@render children?.()}
</button>

<style>
	.mai-button {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: var(--mai-space-2);
		min-height: var(--mai-target-min);
		padding: var(--mai-space-2) var(--mai-space-4);
		border: var(--mai-border-width) solid transparent;
		border-radius: var(--mai-radius-md);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-medium);
		cursor: pointer;
		transition:
			background-color var(--mai-motion-fast) ease,
			border-color var(--mai-motion-fast) ease,
			color var(--mai-motion-fast) ease;
	}

	.mai-button:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	/* Mechanicus cogitator chrome: every accent below resolves from --mai-*
	   tokens only (campaign hard rule: no hardcoded color literals). */

	.mai-button--primary {
		background: var(--mai-color-primary);
		color: var(--mai-color-primary-contrast);
	}

	.mai-button--primary:hover:not(:disabled) {
		background: var(--mai-color-primary-hover);
		/* Subtle brass filigree on the crimson casing. */
		box-shadow: inset 0 0 0 1px var(--mai-color-brass);
	}

	.mai-button--primary:active:not(:disabled) {
		background: var(--mai-color-primary-active);
		box-shadow: inset 0 0 0 1px var(--mai-color-brass-dim);
	}

	.mai-button--secondary {
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		border-color: var(--mai-color-border);
	}

	.mai-button--secondary:hover:not(:disabled) {
		border-color: var(--mai-color-brass);
		background: var(--mai-color-surface-raised);
	}

	.mai-button--secondary:active:not(:disabled) {
		border-color: var(--mai-color-brass-dim);
	}

	.mai-button--danger {
		background: var(--mai-status-danger-solid);
		color: var(--mai-status-solid-contrast);
	}

	.mai-button--danger:hover:not(:disabled) {
		box-shadow: inset 0 0 0 1px var(--mai-status-danger-border);
	}

	.mai-button__spinner {
		width: 1em;
		height: 1em;
		border: 2px solid currentColor;
		border-right-color: transparent;
		border-radius: 50%;
		animation: mai-button-spin var(--mai-motion-base) linear infinite;
	}

	/* Collapsed globally by the reduced-motion baseline in tokens.css; the key
	   stops there, so no per-component duplicate rule is needed. */
	@keyframes mai-button-spin {
		to {
			transform: rotate(1turn);
		}
	}
</style>
