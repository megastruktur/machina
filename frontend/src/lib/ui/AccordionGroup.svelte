<script lang="ts">
	/**
	 * AccordionGroup — reusable collapsible tree/section primitive.
	 *
	 * Used in the Left Side Drawer for:
	 *  - Each Server node in the Multi-Server Accordion Tree (Option B)
	 *  - The Chat History (`Recent Chats`) drawer section
	 *  - The `Fleet & Global` section
	 */
	import type { Snippet } from 'svelte';
	import type { HTMLAttributes } from 'svelte/elements';

	type Props = Omit<HTMLAttributes<HTMLElement>, 'ontoggle'> & {
		/** Section header title. */
		title: string;
		/** Optional leading icon or sigil string. */
		icon?: string;
		/** Optional right-side badge text (e.g. count or server status). */
		badge?: string;
		/** Whether the accordion body is expanded; bindable. */
		open?: boolean;
		/** Optional callback when the header is toggled. */
		ontoggle?: (open: boolean) => void;
		/** Optional test id for the header toggle button. */
		toggleTestId?: string;
		children?: Snippet;
	};

	let {
		title,
		icon = '',
		badge = '',
		open = $bindable(true),
		ontoggle,
		toggleTestId,
		class: className = '',
		children,
		...rest
	}: Props = $props();

	function toggle(): void {
		open = !open;
		ontoggle?.(open);
	}
</script>

<section {...rest} class="mai-accordion {className}" class:mai-accordion--open={open}>
	<button
		type="button"
		class="mai-accordion__header"
		tabindex="-1"
		aria-expanded={open}
		data-testid={toggleTestId}
		onclick={toggle}
	>
		<span class="mai-accordion__chevron" aria-hidden="true">{open ? '▾' : '▸'}</span>
		{#if icon}
			<span class="mai-accordion__icon" aria-hidden="true">{icon}</span>
		{/if}
		<span class="mai-accordion__title">{title}</span>
		{#if badge}
			<span class="mai-accordion__badge">{badge}</span>
		{/if}
	</button>
	{#if open}
		<div class="mai-accordion__body">
			{@render children?.()}
		</div>
	{/if}
</section>

<style>
	.mai-accordion {
		display: flex;
		flex-direction: column;
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md);
		overflow: hidden;
	}

	.mai-accordion--open {
		border-color: var(--mai-color-border-strong);
	}

	.mai-accordion__header {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		width: 100%;
		min-height: 2rem;
		padding: var(--mai-space-2) var(--mai-space-3);
		font-family: var(--mai-font-sans);
		font-size: var(--mai-text-sm);
		font-weight: var(--mai-weight-semibold);
		color: var(--mai-color-text);
		background: transparent;
		border: none;
		text-align: left;
		cursor: pointer;
		transition: background-color var(--mai-motion-fast) ease;
	}

	.mai-accordion__header:hover {
		background: var(--mai-color-surface);
	}

	.mai-accordion__chevron {
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-brass);
		width: 0.75rem;
	}

	.mai-accordion__icon {
		font-size: var(--mai-text-sm);
		line-height: 1;
	}

	.mai-accordion__title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.mai-accordion__badge {
		flex: 0 0 auto;
		padding: 1px var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: 0.7rem;
		color: var(--mai-color-brass);
		background: var(--mai-color-bg);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-pill);
	}

	.mai-accordion__body {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: var(--mai-space-1) var(--mai-space-2) var(--mai-space-2);
		border-top: var(--mai-border-width) solid var(--mai-color-border);
		background: var(--mai-color-surface);
	}
</style>
