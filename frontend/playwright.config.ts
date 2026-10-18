import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	testDir: 'tests',
	fullyParallel: true,
	forbidOnly: !!process.env.CI,
	reporter: process.env.CI ? 'line' : 'list',
	use: {
		...devices['Desktop Chrome'],
		baseURL: 'http://127.0.0.1:4173',
		// Dev container runs without unprivileged user namespaces, so the
		// chromium setuid/userns sandbox cannot start (zygote FATAL "No usable
		// sandbox!", probed 2026-10-13). Disable it for this trusted fixture
		// host; production browsers are unaffected.
		chromiumSandbox: false,
		trace: 'retain-on-failure'
	},
	// Serves the real production build (vite build + vite preview), not a dev
	// server, so every browser proof exercises the same artifacts `pnpm build`
	// ships for the Compose-internal service. `exec` replaces the shell with
	// vite so the teardown signal reaches the server process directly; a
	// pnpm/sh indirection here orphaned vite on shutdown (observed 2026-10-13:
	// runner waits on the surviving grandchild).
	webServer: {
		command: 'vite build && exec vite preview',
		url: 'http://127.0.0.1:4173',
		reuseExistingServer: !process.env.CI,
		timeout: 120_000
	},
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }]
});
