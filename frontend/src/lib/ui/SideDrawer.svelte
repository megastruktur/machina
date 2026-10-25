<script lang="ts">
	/**
	 * SideDrawer — reusable collapsible solid left navigation drawer.
	 *
	 * Owns the sidebar chrome (solid `--mai-color-surface` background, right
	 * border, collapse/expand toggle, and standardized internal paddings) with
	 * slots for `header`, `children` (scrollable navigation tree), and `footer`.
	 */
	import type { Snippet } from 'svelte';
	import type { HTMLAttributes } from 'svelte/elements';

	type Props = HTMLAttributes<HTMLElement> & {
		/** Collapsed state (renders a compact rail when true). */
		collapsed?: boolean;
		/** Optional header slot (brand + collapse button). */
		header?: Snippet;
		/** Main scrollable drawer body (chat controls, server tree, global items). */
		children?: Snippet;
		/** Optional sticky bottom footer slot (status, sign in/out). */
		footer?: Snippet;
	};

	let {
		collapsed = $bindable(false),
		class: className = '',
		header,
		children,
		footer,
		...rest
	}: Props = $props();
</script>

<aside
	{...rest}
	class="mai-side-drawer {className}"
	class:mai-side-drawer--collapsed={collapsed}
>
	<div class="mai-side-drawer__header">
		{#if header}
			{@render header()}
		{/if}
		<button
			type="button"
			class="mai-side-drawer__toggle"
			tabindex="-1"
			aria-expanded={!collapsed}
			aria-label={collapsed ? 'Expand navigation drawer' : 'Collapse navigation drawer'}
			data-testid="side-drawer-toggle"
			onclick={() => (collapsed = !collapsed)}
		>
			{collapsed ? '»' : '«'}
		</button>
	</div>

	<div class="mai-side-drawer__body" hidden={collapsed}>
		{@render children?.()}
	</div>

	{#if footer}
		<div class="mai-side-drawer__footer" hidden={collapsed}>
			{@render footer()}
		</div>
	{/if}
</aside>

<style>
	.mai-side-drawer {
		flex: 0 0 17.5rem;
		width: 17.5rem;
		height: 100%;
		display: flex;
		flex-direction: column;
		background: var(--mai-color-surface);
		color: var(--mai-color-text);
		border-right: var(--mai-border-width) solid var(--mai-color-border);
		box-shadow: var(--mai-shadow-float);
		z-index: 25;
		transition:
			width var(--mai-motion-base) ease,
			flex-basis var(--mai-motion-base) ease;
	}

	.mai-side-drawer--collapsed {
		flex: 0 0 3.25rem;
		width: 3.25rem;
	}

	/* Pocket viewports: an EXPANDED drawer OVERLAYS the workspace instead of
	 * riding the shell flex row — an in-flow expanded drawer squeezed the
	 * stage to an 80px rail at 360×640 and crushed the bottom chat dock
	 * inside it (R-24c: 18px-wide conversation). The COLLAPSED rail stays
	 * in-flow so the dock and windows yield to it exactly as on desktop.
	 * Expand/collapse behavior and the toggle are unchanged; desktop widths
	 * are unaffected. */
	@media (max-width: 640px) {
		.mai-side-drawer:not(.mai-side-drawer--collapsed) {
			position: absolute;
			top: 0;
			bottom: 0;
			left: 0;
		}
	}

	.mai-side-drawer__header {
		flex: 0 0 auto;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-2);
		padding: var(--mai-space-3) var(--mai-card-pad);
		background: var(--mai-color-surface-header);
		border-bottom: var(--mai-border-width) solid var(--mai-color-brass-dim);
	}

	.mai-side-drawer--collapsed .mai-side-drawer__header {
		justify-content: center;
		padding: var(--mai-space-3) var(--mai-space-1);
	}

	.mai-side-drawer__toggle {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: var(--mai-target-min);
		height: var(--mai-target-min);
		padding: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
		background: var(--mai-color-surface-raised);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm);
		cursor: pointer;
	}

	.mai-side-drawer__toggle:hover {
		color: var(--mai-color-text);
		border-color: var(--mai-color-brass);
	}

	.mai-side-drawer__body {
		flex: 1 1 0%;
		min-height: 0;
		overflow-y: auto;
		padding: var(--mai-card-pad);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3);
	}

	.mai-side-drawer__footer {
		flex: 0 0 auto;
		padding: var(--mai-space-2) var(--mai-card-pad);
		background: var(--mai-color-bg);
		border-top: var(--mai-border-width) solid var(--mai-color-border);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
	}
</style>
