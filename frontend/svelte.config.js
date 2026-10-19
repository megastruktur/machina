import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),

	compilerOptions: {
		// Project-wide runes force (added by mui-01-foundation) lifted in the
		// mui-05 fix round: lucide-svelte@1.0.1 ships legacy Svelte-4 components
		// ($$props), which fail to compile under forced runes mode. Svelte 5
		// auto-detects runes per component, so MAI-owned runes-mode code keeps
		// working without the force.
		// runes: true
	},

	kit: {
		// mai-web is a Compose-internal SSR service behind the single TLS-terminating
		// reverse proxy (contracts.md C08 rule 3): server-rendered route shell,
		// client-only workspace after hydration (windowed-workspace ADR D4).
		adapter: adapter()
	}
};

export default config;
