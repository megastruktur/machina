<script lang="ts">
	// Thin route shell (windowed-workspace ADR §2: routes own entry only, no
	// feature logic, no window state, no drag logic, no stores). The reviewed
	// labeled component host stays the mounted landmark of the D4 SSR shell;
	// the composed desktop workspace (fixture registry, workspace state,
	// frames, panel hosts) mounts inside it and lives entirely in the
	// workspace layer. No domain content is embedded here.
	import ComponentHost from '../../tests/fixtures/component-host.svelte';
	import DesktopShell from '$lib/workspace/DesktopShell.svelte';
	import GridCanvas from '$lib/ui/GridCanvas.svelte';
</script>

<svelte:head>
	<title>Machina — M.A.I. Workspace</title>
</svelte:head>

<GridCanvas class="mai-workspace-route">
	<ComponentHost label="Machina workspace">
		<DesktopShell />
	</ComponentHost>
</GridCanvas>

<style>
	/* Zero-scroll 100dvh line-grid workspace route: no top header bar,
	   full viewport given to the SideDrawer + Spatial Canvas stage. */
	:global(.mai-workspace-route) {
		padding: 0;
	}

	:global(.mai-workspace-route .component-host) {
		flex: 1 1 0%;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
</style>
