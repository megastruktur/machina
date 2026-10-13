import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
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
