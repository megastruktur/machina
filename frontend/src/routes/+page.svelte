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

<main class="mai-workspace-route">
	<header class="mai-command-header" data-testid="command-header">
		<div class="mai-command-header__brand">
			<span class="mai-command-header__sigil" aria-hidden="true">⚙</span>
			<h1 class="mai-command-header__title">Machina</h1>
			<span class="mai-command-header__sep" aria-hidden="true">·</span>
			<p class="mai-command-header__subtitle">M.A.I. — Machina Anima Intelligentia</p>
		</div>
	</header>
	<ComponentHost label="Machina workspace">
		<DesktopShell />
	</ComponentHost>
</main>

<style>
	/* Zero-scroll 100dvh Odyssey spatial workspace with standardized global padding. */
	.mai-workspace-route {
		height: 100dvh;
		max-height: 100dvh;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2);
		padding: var(--mai-page-pad, var(--mai-space-3));
		background-color: var(--mai-color-bg);
		background-image: var(--mai-canvas-bg);
		background-size: 100% 100%, 100% 100%, 24px 24px;
		overflow: hidden;
		box-sizing: border-box;
	}

	.mai-workspace-route :global(.component-host) {
		flex: 1 1 0%;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}

	.mai-command-header {
		flex: 0 0 auto;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--mai-space-3);
		padding: 0 var(--mai-space-2);
	}

	.mai-command-header__brand {
		display: inline-flex;
		align-items: center;
		gap: var(--mai-space-2);
		flex-wrap: wrap;
	}

	.mai-command-header__sigil {
		color: var(--mai-color-brass);
		font-size: var(--mai-text-base);
	}

	.mai-command-header__title {
		margin: 0;
		font-family: var(--mai-font-display);
		font-size: var(--mai-text-base);
		font-weight: var(--mai-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--mai-color-text);
	}

	.mai-command-header__sep {
		color: var(--mai-color-brass-dim);
	}

	.mai-command-header__subtitle {
		margin: 0;
		font-family: var(--mai-font-mono);
		font-size: var(--mai-text-xs);
		color: var(--mai-color-text-muted);
	}
</style>
