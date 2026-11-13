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
		/** MCR S3: mark this section as the active one (e.g. the selected
		 * server) — paints ONLY a subtle 2px brass bar on the header. */
		active?: boolean;
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
		active = false,
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

<section
	{...rest}
	class="mai-accordion {className}"
	class:mai-accordion--open={open}
	class:mai-accordion--active={active}
>
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
			<span
				class="mai-accordion__badge"
				class:mai-accordion__badge--active={badge === 'active'}
			>{badge}</span>
		{/if}
	</button>
	{#if open}
		<div class="mai-accordion__body">
			{@render children?.()}
		</div>
	{/if}
</section>

<style>
	/* MCR S3: flush boxless drawer sections — a quiet hairline rule
	   between sections, NO 4-sided boxes, NO raised slabs, NO gold
	   open-box styling. */
	.mai-accordion {
		display: flex;
		flex-direction: column;
		background: transparent;
		border: none;
		border-bottom: 1px solid
			color-mix(in srgb, var(--mai-color-border) 60%, transparent);
		border-radius: 0;
		padding-bottom: var(--mai-space-2);
		overflow: hidden;
	}

	.mai-accordion__header {
		display: flex;
		align-items: center;
		gap: var(--mai-space-2);
		width: 100%;
		min-height: 1.75rem;
		padding: var(--mai-space-1) var(--mai-space-2);
		font-family: var(--mai-font-mono);
		font-size: 0.7rem;
		font-weight: 600;
		letter-spacing: 0.07em;
		text-transform: uppercase;
		color: var(--mai-color-text-muted);
		background: transparent;
		border: none;
		text-align: left;
		cursor: pointer;
		transition:
			background-color var(--mai-motion-fast) ease,
			color var(--mai-motion-fast) ease;
	}

	.mai-accordion__header:hover {
		color: var(--mai-color-text);
		background: color-mix(in srgb, var(--mai-color-surface-raised) 60%, transparent);
	}

	/* MCR S3 single-accent discipline: the ONLY open-state emphasis is
	   text color; the active section adds ONLY a 2px brass left bar. */
	.mai-accordion--open > .mai-accordion__header {
		color: var(--mai-color-text);
	}

	.mai-accordion--active > .mai-accordion__header {
		border-left: 2px solid var(--mai-color-brass);
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

	/* Quiet borderless mono counter — brass only for the active mark. */
	.mai-accordion__badge {
		flex: 0 0 auto;
		padding: 0 2px;
		font-family: var(--mai-font-mono);
		font-size: 0.66rem;
		color: var(--mai-color-text-subtle);
		background: transparent;
		border: none;
	}

	.mai-accordion__badge--active {
		color: var(--mai-color-brass);
	}

	.mai-accordion__body {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 2px 0 0 var(--mai-space-1);
		border-top: none;
		background: transparent;
	}
</style>
