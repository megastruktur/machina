import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mui-06-readme-showcase — README screenshot producer (not a behavior test).
 *
 * Captures the three README showcases from the REAL surfaces:
 *  - `screenshot-login-sanctuary.png`: the `/login` Machina Login Sanctuary
 *    (production route, zero API traffic needed for the unauthenticated form).
 *  - `screenshot-command-deck.png`: the composed DesktopShell route (`/`) —
 *    floating windows open plus the Always-Visible Bottom Chat Dock with its
 *    pinned Server Chip and an `@service` chip (offline in-memory slice, zero
 *    API requests, same convention as the reviewed 01-desktop-shell proofs).
 *  - `screenshot-service-map.png`: the REAL `ApplicationInventoryPanel`
 *    (Service Map) mounted through the REAL PanelHost + registry with a
 *    fixture API — the reviewed mai-08/mui-04 in-memory harness convention —
 *    seeded with categorized records (chips, filter pills, 5-slot runbooks,
 *    Chat @service buttons).
 *
 * Output paths are relative to this file: `docs/assets/` at the repo root.
 * Run with `--workers=1` (three heavy captures share one browser budget).
 */

const ASSETS_DIR = new URL('../../docs/assets/', import.meta.url).pathname;

test.use({ viewport: { width: 1440, height: 900 } });

// ---------------------------------------------------------------------------
// 1 + 2: production routes (`/login` sanctuary, `/` command deck)
// ---------------------------------------------------------------------------

test('captures the Machina Login Sanctuary at /login', async ({ page }) => {
	await page.goto('/login');
	await expect(page.getByTestId('login-form')).toBeVisible();
	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Machina');
	await expect(page.getByTestId('login-emblem')).toBeVisible();
	await expect(page.getByText('M.A.I. — Machina Anima Intelligentia')).toBeVisible();
	await page.screenshot({ path: `${ASSETS_DIR}screenshot-login-sanctuary.png`, fullPage: true });
});

const NOTES = 'fixture-notes';
const PREFS = 'fixture-prefs';

function frame(page: Page, kind: string) {
	return page.locator(`.mai-window-frame[data-panel-kind="${kind}"]`);
}

async function openPanel(page: Page, kind: string): Promise<void> {
	await page.getByTestId(`open-${kind}`).click();
	await expect(frame(page, kind)).toHaveCount(1);
}

test('captures the command deck: windows + bottom chat dock with pinned chips', async ({
	page
}) => {
	await page.goto('/');
	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Machina');
	await openPanel(page, NOTES);
	await openPanel(page, PREFS);

	const dock = page.getByTestId('bottom-chat-dock');
	await expect(dock.getByTestId('chat-server-chip')).toContainText('Server Alpha');

	// Pin an `@service` context chip through the dock's own autocomplete.
	await dock.getByTestId('chat-service-picker-toggle').click();
	await dock.getByTestId('chat-service-option').filter({ hasText: '@enshrouded' }).click();
	await expect(dock.getByTestId('chat-service-chip')).toContainText('@enshrouded');
	await expect(dock.getByTestId('chat-server-chip')).toBeVisible();
	await dock.getByTestId('compose-message').fill('Check the enshrouded server status.');

	await page.screenshot({ path: `${ASSETS_DIR}screenshot-command-deck.png`, fullPage: true });
});

// ---------------------------------------------------------------------------
// 3: Service Map — the real ApplicationInventoryPanel in the reviewed
// in-memory harness (trimmed from applications.spec.ts: same virtual-module
// build, same boundary, one server host, records seeded with map metadata)
// ---------------------------------------------------------------------------

const FIXTURE_URL = '/mai-showcase-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-showcase-app.svelte';
const ENTRY_ID = 'virtual:mai-showcase-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/ApplicationInventoryPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/08-web-apps.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const APP_METRICS = '11111111-1111-4222-8333-444455556666';
const APP_GAMES = '22222222-2222-4222-8333-444455556666';
const APP_AI = '33333333-3333-4222-8333-444455556666';
const APP_INFRA = '44444444-4444-4222-8333-444455556666';

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; isEntry: boolean; fileName: string; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry } from '${REGISTRY}';
	import { ApiError } from '${CLIENT}';
	import { APPLICATIONS_PANEL_KIND, applicationsEntry, applicationsServices } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';

	// Categorized Service Map records: chips, filter pills, runbooks and the
	// Chat @service seam all render from this one mounted inventory page.
	const records = [
		mapped('${APP_METRICS}', 'metrics-stack', 'metrics', '/srv/metrics', 'observability',
			'Prometheus + Grafana metrics and alerting stack.', {
				install: 'docker compose up -d',
				update: 'docker compose pull && docker compose up -d',
				uninstall: 'docker compose down -v',
				backup: 'restic backup /srv/metrics',
				verify: 'curl -fsS localhost:9090/-/healthy'
			}, 2),
		mapped('${APP_AI}', 'ollama-inference', 'ollama', '/opt/ollama', 'ai',
			'Local LLM inference endpoint (llama3.1, qwen2.5).', {
				install: 'docker compose up -d',
				update: 'docker compose pull && docker compose up -d',
				uninstall: 'docker compose down',
				backup: 'tar -czf /var/backups/ollama.tgz /opt/ollama',
				verify: 'curl -fsS localhost:11434/api/tags'
			}, 1),
		mapped('${APP_GAMES}', 'game-server', 'games', '/opt/games', 'games',
			'Enshrouded + Valheim dedicated servers.', undefined, 1),
		mapped('${APP_INFRA}', 'nginx-proxy', 'proxy', '/srv/proxy', 'infrastructure',
			'Reverse proxy and TLS termination for the LAN.', undefined, 3)
	];

	function mapped(applicationId, name, project, dir, category, description, instructions, revision) {
		return {
			application_id: applicationId,
			server_id: SERVER_A,
			name,
			origin: 'compose',
			compose_project: project,
			working_dir: dir,
			unit_name: null,
			external_working_dir: dir,
			external_config_files: [dir + '/compose.yaml'],
			external_environment_files: [dir + '/.env'],
			revision,
			schema_revision: 1,
			sha256: 'a'.repeat(64),
			adopted_at: '2026-10-16T10:00:00Z',
			reconciled_at: '2026-10-16T10:00:00Z',
			stale: false,
			category,
			description,
			instructions
		};
	}

	const api = {
		async listAdopted(serverId, page) {
			return {
				server_id: serverId,
				applications: records.map((record) => JSON.parse(JSON.stringify(record))),
				limit: (page && page.limit) || 50,
				offset: (page && page.offset) || 0,
				total: records.length
			};
		},
		async discover(serverId) {
			throw new ApiError(503, 'unavailable', 'showcase fixture: discovery not armed');
		},
		async adopt(serverId, payload) {
			throw new ApiError(503, 'unavailable', 'showcase fixture: adoption not armed');
		},
		async revoke(serverId, applicationId, payload) {
			throw new ApiError(503, 'unavailable', 'showcase fixture: revoke not armed');
		}
	};

	// Lifecycle + docs seams: inert recorders (never invoked in the capture).
	function inert() {}

	const services = applicationsServices(
		api,
		{ openInstall: inert, openUpdate: inert, openRemove: inert },
		{ openDocument: inert }
	);

	const registryResult = createPanelRegistry([applicationsEntry()]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;

	function descriptor(windowId, scope, z) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: APPLICATIONS_PANEL_KIND,
			scope,
			geometry: { x: 8, y: 8, width: 1120, height: 800 },
			presentation: 'normal',
			z,
			title_override: null
		};
	}

	function onPanelError(windowId, panelKind, error) {
		console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error);
	}
</script>

<section data-testid="host-a" aria-label="Service Map host">
	<ComponentHost label="Service Map host">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('9d9d9d9d-9999-4222-8333-444455556666', { kind: 'server', server_id: SERVER_A }, 1)}
			{services}
		/>
	</ComponentHost>
</section>
`;

const ENTRY_SOURCE = `
import { mount } from 'svelte';
import App from '${APP_ID}';
import '${TOKENS}';
mount(App, { target: document.getElementById('app') });
`;

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-showcase-harness',
		resolveId(id) {
			// .svelte virtual modules must NOT get the conventional `\0` prefix:
			// vite-plugin-svelte excludes `\0` ids from Svelte compilation
			// (panel-host harness convention). The JS entry keeps the `\0`.
			if (id === APP_ID) return id;
			if (id === ENTRY_ID) return `\\0${id}`;
			return undefined;
		},
		load(id) {
			if (id === APP_ID) return sources[APP_ID];
			if (id === `\\0${ENTRY_ID}`) return sources[ENTRY_ID];
			return undefined;
		}
	};
}

let harnessJs = '';
let harnessCss = '';
let chunks: Array<{ fileName: string; code: string }> = [];

test.beforeAll(async () => {
	const sources: Record<string, string> = {
		[APP_ID]: APP_SOURCE,
		[ENTRY_ID]: ENTRY_SOURCE
	};
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(sources)],
		build: {
			write: false,
			emptyOutDir: true,
			cssCodeSplit: false,
			minify: false,
			outDir: 'node_modules/.mai-showcase-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'showcase-[hash].js',
					assetFileNames: 'demo[extname]'
				}
			}
		}
	});

	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	chunks = [];
	harnessJs = '';
	harnessCss = '';
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') {
				if (item.isEntry) harnessJs += item.code;
				else chunks.push({ fileName: item.fileName, code: item.code });
			} else if (item.fileName.endsWith('.css')) {
				harnessCss += String(item.source);
			}
		}
	}
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete: missing JS entry chunk or CSS asset');
	}
	if (chunks.length === 0) {
		throw new Error('harness bundle incomplete: the panel must build as a lazy chunk');
	}
});

test('captures the categorized Service Map panel', async ({ page }) => {
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI Service Map fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script>
</body>
</html>`;

	await page.route('**/*', (route) => {
		const { pathname } = new URL(route.request().url());
		if (!pathname.startsWith(FIXTURE_URL)) return route.abort();
		const rest = pathname.slice(FIXTURE_URL.length);
		if (rest === '' || rest.endsWith('/')) {
			return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
		}
		if (rest === 'demo.js') {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (rest === 'demo.css') {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		const chunk = chunks.find((candidate) => candidate.fileName === rest);
		if (chunk) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: chunk.code });
		}
		return route.abort();
	});

	await page.goto(FIXTURE_URL);

	const host = page.getByTestId('host-a');
	await expect(host.getByTestId('apps-root')).toBeVisible();
	const rows = host.getByTestId('inventory-row');
	await expect(rows).toHaveCount(4);
	await expect(host.getByTestId('category-filter-pill')).toHaveCount(5);
	const metricsRow = rows.filter({ hasText: 'metrics-stack' });
	await expect(metricsRow.getByTestId('service-category-chip')).toHaveAttribute(
		'data-category',
		'observability'
	);
	await expect(metricsRow.getByTestId('runbook-step')).toHaveCount(5);
	await expect(
		rows.filter({ hasText: 'ollama-inference' }).getByTestId('service-chat-trigger')
	).toBeVisible();

	await page.screenshot({ path: `${ASSETS_DIR}screenshot-service-map.png`, fullPage: true });
});
