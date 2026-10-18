<script lang="ts">
	// Thin route shell (windowed-workspace ADR §2: routes own entry only, no
	// feature logic, no window state, no drag logic, no stores). The reviewed
	// labeled component host stays the mounted landmark of the D4 SSR shell;
	// the composed desktop workspace (fixture registry, workspace state,
	// frames, panel hosts) mounts inside it and lives entirely in the
	// workspace layer. No domain content is embedded here.
	import ComponentHost from '../../tests/fixtures/component-host.svelte';
	import DesktopShell from '$lib/workspace/DesktopShell.svelte';
</script>

<svelte:head>
	<title>MAI</title>
</svelte:head>

<main>
	<h1>MAI</h1>
	<p>Desktop workspace shell — fixture panels until a live session signs in.</p>
	<ComponentHost label="MAI workspace">
		<DesktopShell />
	</ComponentHost>
</main>
