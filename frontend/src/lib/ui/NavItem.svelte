<script lang="ts">
	/**
	 * NavItem — reusable sidebar navigation button primitive.
	 *
	 * Used across `ServerTreeNav`, `ChatDrawerSection`, and global drawer actions
	 * so every menu item in the Left Side Drawer shares consistent padding,
	 * hover/active styling, and optional screen-reader prefix/suffix spans.
	 */
	import type { HTMLButtonAttributes } from 'svelte/elements';

	type Props = HTMLButtonAttributes & {
		/** Visible short label of the navigation item (e.g. "Service Map"). */
		label: string;
		/** Optional leading glyph/icon. */
		icon?: string;
		/** Optional right-side meta tag/badge. */
		meta?: string;
		/** Highlight as active/selected item. */
		active?: boolean;
		/** Optional screen-reader-only prefix (keeps Playwright `toHaveText` contracts green). */
		srPrefix?: string;
		/** Optional screen-reader-only suffix (keeps Playwright `toHaveText` contracts green). */
		srSuffix?: string;
	};

	let {
		label,
		icon = '•',
		meta = '',
		active = false,
		srPrefix = '',
		srSuffix = '',
		type = 'button',
		class: className = '',
		...rest
	}: Props = $props();
</script>

<button
	{...rest}
	{type}
	class="mai-nav-item {className}"
	class:mai-nav-item--active={active}
>
	<span class="mai-nav-item__icon" aria-hidden="true">{icon}</span>
	<span class="mai-nav-item__label">
		{#if srPrefix}<span class="mai-sr-only">{srPrefix}</span>{/if}{label}{#if srSuffix}<span class="mai-sr-only">{srSuffix}</span>{/if}
	</span>
	{#if meta}
		<span class="mai-nav-item__meta" aria-hidden="true">{meta}</span>
	{/if}
</button>

<style>
	.mai-nav-item {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		width: 100%;
		min-height: 1.85rem;
		padding: var(--mai-space-1) var(--mai-space-2);
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-xs);
		font-weight: var(--mai-weight-medium);
		color: var(--mai-color-text);
		background: transparent;
		border: none;
		border-left: 2px solid transparent;
		border-radius: var(--mai-radius-sm);
		text-align: left;
		cursor: pointer;
		transition:
			background-color var(--mai-motion-fast) ease,
			border-color var(--mai-motion-fast) ease,
			color var(--mai-motion-fast) ease;
	}

	.mai-nav-item:hover:not(:disabled) {
		background: var(--mai-color-surface-raised);
		border-left-color: var(--mai-color-border);
	}

	.mai-nav-item--active {
		background: var(--mai-color-surface-raised);
		border-left-color: var(--mai-color-brass);
		color: var(--mai-color-text);
	}

	/* MCR S3 single accent: the glyph stays subtle until the row is
	   hovered or active — brass is the ONE accent, spent sparingly. */
	.mai-nav-item__icon {
		flex: 0 0 1rem;
		text-align: center;
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-subtle);
	}

	.mai-nav-item:hover:not(:disabled) .mai-nav-item__icon,
	.mai-nav-item--active .mai-nav-item__icon {
		color: var(--mai-color-brass);
	}

	.mai-nav-item__label {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.mai-nav-item__meta {
		flex: 0 0 auto;
		font-family: var(--mai-font-mono);
		font-size: 0.68rem;
		color: var(--mai-color-text-muted);
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
