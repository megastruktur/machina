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
	<title>Machina — M.A.I. Workspace</title>
</svelte:head>

<main>
	<header class="mai-command-header" data-testid="command-header">
		<h1 class="mai-command-header__title">Machina</h1>
		<p class="mai-command-header__subtitle">M.A.I. — Machina Anima Intelligentia</p>
	</header>
	<ComponentHost label="Machina workspace">
		<DesktopShell />
	</ComponentHost>
</main>

<style>
	/* Tokens only (windowed-workspace ADR §2: the route owns entry markup,
	 * no theme values of its own). */
	.mai-command-header {
		display: flex;
		align-items: baseline;
		gap: var(--mai-space-3);
		flex-wrap: wrap;
		margin: 0 0 var(--mai-space-2);
	}

	.mai-command-header__title {
		margin: 0;
		font-size: var(--mai-text-xl);
		letter-spacing: 0.02em;
	}

	.mai-command-header__subtitle {
		margin: 0;
		font-size: var(--mai-text-sm);
		color: var(--mai-color-text-muted);
	}
</style>
