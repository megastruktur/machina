<script lang="ts">
	/**
	 * Chip — shared reusable pill badge / context chip primitive (Odyssey spatial UI).
	 *
	 * Used across the Bottom Chat Dock (Server Chip, `@service` chips, General
	 * Consult badge), WindowFrame context badges, and panel category/status tags
	 * so pill geometry, padding, lock indicator, and remove button live in one
	 * component.
	 */
	import type { Snippet } from 'svelte';
	import type { HTMLAttributes } from 'svelte/elements';

	type Variant = 'default' | 'brass' | 'primary' | 'info' | 'success' | 'warning' | 'danger' | 'neutral';

	type Props = HTMLAttributes<HTMLSpanElement> & {
		/** Visual tone of the chip. */
		variant?: Variant;
		/** Immutable/locked state (sets `data-locked="true"` when true). */
		locked?: boolean;
		/** Optional remove handler; renders a `×` button when provided and not locked. */
		onremove?: () => void;
		/** Accessible label for the remove button. */
		removeLabel?: string;
		/** `data-testid` for the remove button. */
		removeTestId?: string;
		/** Chip content. */
		children?: Snippet;
	};

	let {
		variant = 'default',
		locked = false,
		onremove,
		removeLabel = 'Remove',
		removeTestId,
		class: className = '',
		children,
		...rest
	}: Props = $props();
</script>

<span
	{...rest}
	class="mai-chip mai-chip--{variant} {className}"
	class:mai-chip--locked={locked}
	data-locked={locked ? 'true' : null}
>
	<span class="mai-chip__content">
		{@render children?.()}
	</span>
	{#if onremove && !locked}
		<button
			type="button"
			class="mai-chip__remove"
			aria-label={removeLabel}
			data-testid={removeTestId}
			onclick={onremove}
		>
			×
		</button>
	{/if}
</span>

<style>
	.mai-chip {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1);
		min-height: 24px;
		padding: 2px var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		letter-spacing: 0.03em;
		line-height: var(--mai-leading-tight);
		color: var(--mai-color-text);
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-left-width: var(--mai-chip-accent-width, 3px);
		border-left-color: var(--mai-color-brass-dim);
		border-radius: var(--mai-radius-sm);
		white-space: nowrap;
		transition:
			border-color var(--mai-motion-fast) ease,
			background-color var(--mai-motion-fast) ease;
	}

	.mai-chip--brass {
		border-color: var(--mai-color-border-strong);
		border-left-color: var(--mai-color-brass);
		color: var(--mai-color-text);
	}

	.mai-chip--primary {
		border-color: var(--mai-color-primary);
		border-left-color: var(--mai-color-primary-hover);
		background: var(--mai-status-danger-bg);
		color: var(--mai-color-text);
	}

	.mai-chip--info {
		color: var(--mai-status-info-text);
		background: var(--mai-status-info-bg);
		border-color: var(--mai-status-info-border);
	}

	.mai-chip--success {
		color: var(--mai-status-success-text);
		background: var(--mai-status-success-bg);
		border-color: var(--mai-status-success-border);
	}

	.mai-chip--warning {
		color: var(--mai-status-warning-text);
		background: var(--mai-status-warning-bg);
		border-color: var(--mai-status-warning-border);
	}

	.mai-chip--danger {
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
		border-color: var(--mai-status-danger-border);
	}

	.mai-chip--neutral {
		color: var(--mai-status-neutral-text);
		background: var(--mai-status-neutral-bg);
		border-color: var(--mai-status-neutral-border);
	}

	.mai-chip--locked {
		border-style: solid;
		border-color: var(--mai-color-brass-dim);
	}

	.mai-chip__content {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-1);
	}

	.mai-chip__remove {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 16px;
		height: 16px;
		padding: 0;
		margin-left: 2px;
		font-size: var(--mai-text-xs);
		line-height: 1;
		color: var(--mai-color-text-muted);
		background: transparent;
		border: none;
		border-radius: var(--mai-radius-pill);
		cursor: pointer;
		transition:
			background-color var(--mai-motion-fast) ease,
			color var(--mai-motion-fast) ease;
	}

	.mai-chip__remove:hover {
		background: var(--mai-status-danger-bg);
		color: var(--mai-status-danger-text);
	}
</style>
