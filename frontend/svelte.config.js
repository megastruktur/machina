import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),

	compilerOptions: {
		// MAI-owned window/workspace code is Svelte 5 runes throughout
		// (windowed-workspace ADR D2); the official Svelte template recommends
		// forcing runes mode project-wide.
		runes: true
	},

	kit: {
		// mai-web is a Compose-internal SSR service behind the single TLS-terminating
		// reverse proxy (contracts.md C08 rule 3): server-rendered route shell,
		// client-only workspace after hydration (windowed-workspace ADR D4).
		adapter: adapter()
	}
};

export default config;
