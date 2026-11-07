import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),

	compilerOptions: {
		// MAI-owned window/workspace code and virtual test fixtures are compiled
		// in Svelte 5 runes mode throughout (windowed-workspace ADR D2).
		runes: true
	},

	vitePlugin: {
		// Third-party legacy Svelte 4 packages in node_modules (e.g.
		// lucide-svelte@1.0.1 using $$props) opt out of forced runes mode
		// via dynamicCompileOptions — but ONLY files with actual legacy
		// syntax (`$$props` / `export let` / `$:`). Forcing runes:false on
		// Svelte 5 runes packages (virtua@0.54) mis-compiles their
		// $props/$state runes into legacy store auto-subscriptions and
		// crashes with "props is not defined" at runtime (CVM-S1).
		dynamicCompileOptions({ filename, code }) {
			if (
				filename.includes('node_modules') &&
				/\$\$props|\$\$restProps|\$\$slots|export\s+let|\$:\s/.test(code)
			) {
				return { runes: false };
			}
		}
	},

	kit: {
		// mai-web is a Compose-internal SSR service behind the single TLS-terminating
		// reverse proxy (contracts.md C08 rule 3): server-rendered route shell,
		// client-only workspace after hydration (windowed-workspace ADR D4).
		adapter: adapter()
	}
};

export default config;
