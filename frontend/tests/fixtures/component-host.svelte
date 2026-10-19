<script lang="ts">
	/**
	 * Inert, labeled component host — the shared real-browser mount fixture named by
	 * the test policy ("Early UI bootstrap") and the runtime-scenarios WS rows.
	 *
	 * Owns nothing: no window state, no drag/geometry logic, no server scoping, no
	 * global mutation target, no module-level `window`/`document`/`localStorage`
	 * access (windowed-workspace ADR §5 rule 4). Safe to render on the server and
	 * mount anywhere. The only state it ever touches is its own `data-mounted`
	 * marker, flipped by a client-only effect so a browser test can prove an
	 * actual mount rather than static HTML.
	 */
	import type { Snippet } from 'svelte';

	let {
		label = 'Machina component host',
		children
	}: { label?: string; children?: Snippet } = $props();

	let host = $state<HTMLElement | null>(null);

	$effect(() => {
		if (host) {
			host.dataset.mounted = 'true';
		}
	});
</script>

<section
	bind:this={host}
	 class="component-host"
	 data-testid="component-host"
	 data-mounted="false"
	 aria-label={label}
>
	<span data-testid="component-host-label" class="component-host__label">{label}</span>
	{#if children}
		{@render children()}
	{/if}
</section>

<style>
	/* The label is the redundant text twin of the section's aria-label: kept
	 * in the DOM for tests and assistive tech, visually hidden so it cannot
	 * disturb a host's layout. No theme values. */
	.component-host__label {
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
