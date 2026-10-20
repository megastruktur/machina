<script lang="ts">
	/**
	 * Shared button primitive (card 01-ui-controls, P01; Odyssey spatial UI upgrade).
	 *
	 * Layer: shared UI primitive (windowed-workspace ADR-0001 §2). Owns nothing:
	 * no window geometry, no drag, no server scoping, no stores, no module-level
	 * `window`/`document` access (ADR §5 rule 4). Safe to render on the server.
	 *
	 * Accessibility: a native <button> is used as-is — native Enter/Space
	 * activation, native disabled semantics, and the global `:focus-visible`
	 * focus ring from ui/tokens.css (ADR §6).
	 */
	import type { Snippet } from 'svelte';
	import type { HTMLButtonAttributes } from 'svelte/elements';

	type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';
	type Size = 'sm' | 'md';

	type Props = HTMLButtonAttributes & {
		/** Visual variant; defaults to the primary action style. */
		variant?: Variant;
		/** Density size; `sm` for HUD/dock bars, `md` default. */
		size?: Size;
		/** Pill shape (rounded-full) for modern Odyssey floating controls. */
		pill?: boolean;
		/** Pending state: disables the button and sets aria-busy. */
		loading?: boolean;
		/** Button label content. */
		children?: Snippet;
	};

	let {
		variant = 'primary',
		size = 'md',
		pill = false,
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
	class="mai-button mai-button--{variant} mai-button--{size} {className}"
	class:mai-button--pill={pill}
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
		padding: var(--mai-control-pad-y) var(--mai-control-pad-x);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.04em;
		text-transform: uppercase;
		white-space: nowrap;
		cursor: pointer;
		transition:
			background-color var(--mai-motion-fast) ease,
			border-color var(--mai-motion-fast) ease,
			color var(--mai-motion-fast) ease,
			box-shadow var(--mai-motion-fast) ease;
	}

	.mai-button--sm {
		padding: var(--mai-space-1) var(--mai-space-3);
		font-size: var(--mai-text-xs);
	}

	.mai-button--pill {
		border-radius: var(--mai-radius-md);
	}

	.mai-button:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	/* Mechanicus / Odyssey chrome: every accent resolves from --mai-* tokens.
	   Because CSS `clip-path` slices away standard rectangular `border` and
	   `box-shadow: inset` along the 45° top-right and bottom-left chamfers,
	   chipped variants (.mai-button--primary and .mai-button--danger) paint a
	   full 6-edge polygon border via ::before (border plate) and ::after
	   (1px-inset inner fill plate) while keeping `background-color` on the
	   button element itself for token inspection. */
	.mai-button--primary,
	.mai-button--danger {
		--mai-button-inner-clip: polygon(
			0 0,
			calc(100% - 5.5px) 0,
			100% 5.5px,
			100% 100%,
			5.5px 100%,
			0 calc(100% - 5.5px)
		);
		position: relative;
		isolation: isolate;
		border-color: transparent;
		clip-path: var(--mai-button-clip, none);
	}

	.mai-button--primary::before,
	.mai-button--danger::before {
		content: '';
		position: absolute;
		inset: -1px;
		z-index: -2;
		background: var(--mai-btn-border, var(--mai-color-brass-dim));
		clip-path: var(--mai-button-clip, none);
		border-radius: inherit;
		pointer-events: none;
		transition: background-color var(--mai-motion-fast) ease;
	}

	.mai-button--primary::after,
	.mai-button--danger::after {
		content: '';
		position: absolute;
		inset: 0;
		z-index: -1;
		background: var(--mai-btn-fill, var(--mai-color-primary));
		box-shadow: inset 0 1px 0 var(--mai-border-specular);
		clip-path: var(--mai-button-inner-clip, none);
		border-radius: inherit;
		pointer-events: none;
		transition: background-color var(--mai-motion-fast) ease;
	}

	:global([data-theme='standard']) .mai-button--primary,
	:global([data-theme='standard']) .mai-button--danger {
		--mai-button-inner-clip: none;
	}

	.mai-button--primary {
		--mai-btn-fill: var(--mai-color-primary);
		--mai-btn-border: var(--mai-color-brass-dim);
		background-color: var(--mai-color-primary);
		color: var(--mai-color-primary-contrast);
	}

	.mai-button--primary:hover:not(:disabled) {
		--mai-btn-fill: var(--mai-color-primary-hover);
		--mai-btn-border: var(--mai-color-brass);
		background-color: var(--mai-color-primary-hover);
	}

	.mai-button--primary:active:not(:disabled) {
		--mai-btn-fill: var(--mai-color-primary-active);
		--mai-btn-border: var(--mai-color-brass-dim);
		background-color: var(--mai-color-primary-active);
	}

	.mai-button--secondary {
		background: var(--mai-surface-glass-raised, var(--mai-color-surface));
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

	.mai-button--ghost {
		background: transparent;
		color: var(--mai-color-text-muted);
		border-color: transparent;
	}

	.mai-button--ghost:hover:not(:disabled) {
		color: var(--mai-color-text);
		background: var(--mai-surface-glass-raised, var(--mai-color-surface-raised));
		border-color: var(--mai-color-border);
	}

	.mai-button--danger {
		--mai-btn-fill: var(--mai-status-danger-solid);
		--mai-btn-border: var(--mai-color-brass-dim);
		background-color: var(--mai-status-danger-solid);
		color: var(--mai-status-solid-contrast);
	}

	.mai-button--danger:hover:not(:disabled) {
		--mai-btn-border: var(--mai-status-danger-text);
	}

	.mai-button__spinner {
		width: 1em;
		height: 1em;
		border: 2px solid currentColor;
		border-right-color: transparent;
		border-radius: 50%;
		animation: mai-button-spin var(--mai-motion-base) linear infinite;
	}

	@keyframes mai-button-spin {
		to {
			transform: rotate(1turn);
		}
	}
</style>
