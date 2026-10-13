<script lang="ts">
	// Root layout — the single load point for the shared token layer
	// (windowed-workspace ADR §6; brief acceptance 1: one shared
	// spacing/color/type/focus layer, loaded once, no duplicated theme rules).
	// Routes stay thin (ADR §2): no window state, no drag logic, no stores,
	// no module-level browser access — safe to server-render per request
	// (ADR §5); the workspace mounts client-side in later cards.
	import type { Snippet } from 'svelte';
	import '$lib/ui/tokens.css';

	let { children }: { children?: Snippet } = $props();
</script>

{@render children?.()}
