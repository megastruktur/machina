import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	// Tailwind CSS v4 (CSS-first, processed by the dedicated Vite plugin) in
	// addition to the SvelteKit plugin. The token layer
	// (src/lib/ui/tokens.css) imports `tailwindcss` + the Skeleton base and is
	// loaded exactly once by src/routes/+layout.svelte.
	plugins: [tailwindcss(), sveltekit()],
	// Compose-internal service (contracts.md C08 rule 3): loopback HTTP is
	// development-only. Bind the preview server explicitly to IPv4 loopback so
	// `pnpm preview` and the Playwright webServer/baseURL (127.0.0.1:4173)
	// always agree, independent of how `localhost` resolves (::1 vs 127.0.0.1).
	preview: {
		host: '127.0.0.1',
		port: 4173,
		strictPort: true
	}
});
