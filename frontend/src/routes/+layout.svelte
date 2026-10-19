<script lang="ts">
	// Root layout — the single load point for fonts and the shared token layer
	// (Encapsulated Theme Contract, campaign mechanicus-ui; windowed-workspace
	// ADR §6; brief acceptance 1: one shared spacing/color/type/focus layer,
	// loaded once, no duplicated theme rules).
	//
	// Self-hosted offline webfonts (loaded once, bundled by Vite — no external
	// font CDN at runtime): Cinzel 700 for display headings, Rajdhani
	// 500/600/700 as the UI voice, JetBrains Mono 400 for code/data.
	import '@fontsource/cinzel/700.css';
	import '@fontsource/rajdhani/500.css';
	import '@fontsource/rajdhani/600.css';
	import '@fontsource/rajdhani/700.css';
	import '@fontsource/jetbrains-mono/400.css';
	// Token layer: imports Tailwind v4 base, the Skeleton base, and the theme
	// layers (mechanicus default on :root, standard override), then bridges
	// structural --mai-* tokens and the safe global base styles. Theme
	// switching is purely `data-theme` on <html> — there is deliberately NO
	// theme-switcher UI here (mui_01-foundation non-goal).
	import '$lib/ui/tokens.css';

	import type { Snippet } from 'svelte';

	let { children }: { children?: Snippet } = $props();
</script>

{@render children?.()}
