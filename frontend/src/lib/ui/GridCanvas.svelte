<script lang="ts">
	/**
	 * GridCanvas — reusable full-viewport (100dvh × 100vw) architectural
	 * line-grid workspace container.
	 *
	 * Renders the orthogonal line grid on the base background while keeping
	 * the canvas itself transparent so only floating windows, the left side
	 * drawer, and the bottom chat dock have solid backgrounds.
	 */
	import type { Snippet } from 'svelte';
	import type { HTMLAttributes } from 'svelte/elements';

	type Props = HTMLAttributes<HTMLElement> & {
		as?: 'main' | 'div' | 'section';
		children?: Snippet;
	};

	let { as = 'main', class: className = '', children, ...rest }: Props = $props();
</script>

<svelte:element this={as} {...rest} class="mai-grid-canvas {className}">
	{@render children?.()}
</svelte:element>

<style>
	.mai-grid-canvas {
		height: 100dvh;
		max-height: 100dvh;
		width: 100%;
		display: flex;
		flex-direction: column;
		background-color: var(--mai-color-bg);
		background-image: var(--mai-canvas-bg);
		background-size: var(--mai-canvas-size, 32px 32px);
		background-position: 0 0;
		color: var(--mai-color-text);
		overflow: hidden;
		box-sizing: border-box;
	}
</style>
