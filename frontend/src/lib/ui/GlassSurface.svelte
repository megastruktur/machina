<script lang="ts">
	/**
	 * GlassSurface — shared translucent floating container primitive (Odyssey UI).
	 *
	 * Centralizes the glassmorphic backdrop blur (`--mai-blur-glass`), specular
	 * border, elevation (`--mai-shadow-float`), and standardized global paddings
	 * (`--mai-hud-pad-*` and `--mai-card-pad`) for:
	 *  - `hud`: Top floating command bar
	 *  - `dock`: Bottom-center floating Odyssey chat pill
	 *  - `popover`: Floating menus, enrollment drawer, history & transcript popovers
	 *  - `card`: Glassmorphic cards (e.g. Login Sanctuary card)
	 */
	import type { Snippet } from 'svelte';
	import type { HTMLAttributes } from 'svelte/elements';

	type Variant = 'hud' | 'dock' | 'popover' | 'card';

	type Props = HTMLAttributes<HTMLElement> & {
		/** Semantic HTML tag (`div`, `section`, `header`, `nav`, `aside`). */
		as?: 'div' | 'section' | 'header' | 'nav' | 'aside';
		/** Spatial surface variant controlling radius and standardized padding. */
		variant?: Variant;
		children?: Snippet;
	};

	let {
		as = 'div',
		variant = 'card',
		class: className = '',
		children,
		...rest
	}: Props = $props();
</script>

<svelte:element
	this={as}
	{...rest}
	class="mai-glass-surface mai-glass-surface--{variant} {className}"
>
	{@render children?.()}
</svelte:element>

<style>
	.mai-glass-surface {
		background: var(--mai-surface-glass, var(--mai-color-surface));
		backdrop-filter: var(--mai-blur-glass);
		-webkit-backdrop-filter: var(--mai-blur-glass);
		border: var(--mai-border-width) solid var(--mai-color-border);
		box-shadow: var(--mai-shadow-float);
		color: var(--mai-color-text);
	}

	.mai-glass-surface--hud {
		padding: var(--mai-hud-pad-y) var(--mai-hud-pad-x);
		border-radius: var(--mai-radius-xl);
	}

	.mai-glass-surface--dock {
		padding: var(--mai-hud-pad-y) var(--mai-hud-pad-x);
		border-radius: var(--mai-radius-2xl);
		border-color: var(--mai-color-border-strong);
	}

	.mai-glass-surface--popover {
		padding: var(--mai-card-pad);
		border-radius: var(--mai-radius-xl);
		background: var(--mai-surface-glass-raised, var(--mai-color-surface-raised));
	}

	.mai-glass-surface--card {
		padding: var(--mai-panel-pad);
		border-radius: var(--mai-radius-xl);
	}
</style>
