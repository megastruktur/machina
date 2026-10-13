<script lang="ts">
	// Thin route shell (windowed-workspace ADR §2: routes own entry only, no feature
	// logic). The only rendered element is the reusable labeled component host
	// (the empty-workspace region of the D4 shell); the workspace itself mounts
	// client-side in later cards. tests/component-host.spec.ts proves this host
	// really mounts in a browser after hydration.
	import ComponentHost from '../../tests/fixtures/component-host.svelte';
</script>

<svelte:head>
	<title>MAI</title>
</svelte:head>

<main>
	<h1>MAI</h1>
	<p>Desktop workspace shell — no panels are implemented yet.</p>
	<ComponentHost label="MAI workspace" />
</main>
