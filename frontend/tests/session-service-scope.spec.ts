import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * session-service-scope.spec.ts — SCOPE-3 proofs (chat-recovery §B):
 *
 * 1. Header chips render the ACTIVE session's STORED scope: `Server: <name>`
 *    or `General Consult`, `Service: @<name>` when linked — never the
 *    drawer's selection, never a borrowed label.
 * 2. Honest degradation: an unknown enrolled server renders the short UUID;
 *    a deleted map entry renders the explicit `(unavailable)` fallback.
 * 3. The @service picker exists ONLY for a NEW session with honest
 *    loading/empty/error states and a bounded "Load more" continuation;
 *    the selection commits at CREATE (`service_id` = the option's canonical
 *    application_id) and disappears once a session is open (no rebind).
 * 4. 360×640: long service names wrap inside the chip — zero horizontal
 *    overflow.
 *
 * Deterministic component harness (the reviewed session-virtual-history
 * pattern): the REAL SessionChatPanel over a scripted services bag — no
 * backend, no fixtures in the bag under test.
 */

const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const FIXTURE_URL = '/mai-session-scope-fixture/';
const SCOPE_ID = 'virtual:mai-session-scope-entry';
const PANEL = `${FRONTEND_ROOT}src/lib/panels/SessionChatPanel.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

const GLOBAL_SERVER_ID = '00000000-0000-0000-0000-000000000000';
const SERVER_A_ID = '5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60';
const SERVICE_ID = '3f9d2c4e-0001-4001-8001-00000000d001';

/** Node globals (ambient: the project tsconfig targets browser-first code). */
declare const process: { env: Record<string, string | undefined> };

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

declare global {
	interface Window {
		__scope?: {
			sessionId: string | null;
			serverId: string | null;
			services: Record<string, unknown>;
			detail: Record<string, unknown> | null;
			detailError: boolean;
			createLog: Array<Record<string, unknown>>;
			loadMoreLog: number[];
		};
		__mount?(patch: Record<string, unknown>): Promise<void>;
	}
}

function harnessEntry(): string {
	return `
import { mount, unmount } from 'svelte';
import SessionChatPanel from ${JSON.stringify(PANEL)};
import ${JSON.stringify(TOKENS)};

const state = {
	sessionId: null,
	serverId: null,
	services: {},
	detail: null,
	detailError: false,
	createLog: [],
	loadMoreLog: []
};
window.__scope = state;

const TURN_OUTCOME = {
	session_id: '22222222-2222-4222-8222-222222222222',
	run_id: '33333333-3333-4333-8333-333333333333',
	entry_id: '44444444-4444-4444-8444-444444444444',
	seq: 1,
	run_state: 'queued',
	deduplicated: false
};

const api = {
	readAgentSession: async (sessionId, serverId) => {
		if (state.detailError) {
			throw { code: 'unavailable', message: 'detail read failed' };
		}
		if (state.detail === null) {
			throw { code: 'session_not_found', message: 'no session' };
		}
		return { session_id: sessionId, server_id: serverId, ...state.detail };
	},
	createAgentSession: async (payload) => {
		state.createLog.push({ ...payload });
		return {
			session_id: '22222222-2222-4222-8222-222222222222',
			server_id: payload.server_id,
			service_id: payload.service_id ?? null,
			service_name: null,
			source: 'interactive',
			status: 'idle',
			title: null,
			latest_seq: null,
			total_entries: 0,
			compaction_count: 0,
			created_at: '2026-01-01T00:00:00Z',
			updated_at: '2026-01-01T00:00:00Z'
		};
	},
	listAgentSessions: async () => ({ items: [], limit: 10, offset: 0, total: 0, has_more: false }),
	listAgentSessionEntries: async () => ({
		items: [], limit: 30, offset: 0, total: 0, has_more: false,
		has_older: false, oldest_seq: null, latest_seq: null,
		byte_size: 0, truncated_by_bytes: false, next_before_seq: null
	}),
	renameAgentSession: async () => ({}),
	submitAgentSessionTurn: async () => ({ ...TURN_OUTCOME }),
	submitAgentSessionControl: async () => ({ accepted: true })
};

let app = null;
async function render() {
	if (app !== null) unmount(app);
	app = mount(SessionChatPanel, {
		target: document.getElementById('app'),
		props: {
			panel: {
				descriptor: {
					schema_version: 1,
					window_id: 'scope-harness',
					panel_kind: 'chat',
					scope: { kind: 'server', server_id: '00000000-0000-0000-0000-000000000000' },
					geometry: { x: 0, y: 0, width: 0, height: 0 },
					presentation: 'normal',
					z: 0,
					title_override: null
				},
				scope: { kind: 'server', server_id: '00000000-0000-0000-0000-000000000000' },
				services: {
					api,
					serverOptions: [],
					...state.services
				},
				onCleanup: () => () => {}
			},
			sessionId: state.sessionId,
			serverId: state.serverId,
			onsessionopened: (sessionId, serverId) => {
				// The shell attaches the channel + swaps the visible session
				// synchronously; the linked chip then renders from the stored
				// header (the draft picker closes — no silent rebind).
				state.sessionId = sessionId;
				state.serverId = serverId;
				const created = state.createLog.at(-1);
				state.detail = {
					server_id: serverId,
					service_id: created && created.service_id != null ? created.service_id : null,
					service_name: null
				};
				void render();
			}
		}
	});
}

window.__mount = async (patch) => {
	Object.assign(state, patch);
	await render();
};

await render();
`;
}

let harnessJs = '';
let harnessCss = '';

function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-session-scope-harness',
		resolveId(id) {
			if (id === SCOPE_ID) return `\0${SCOPE_ID}`;
			return undefined;
		},
		load(id) {
			if (id === `\0${SCOPE_ID}`) return code;
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(harnessEntry())],
		resolve: { alias: { $lib: `${FRONTEND_ROOT}src/lib` } },
		build: {
			write: false,
			emptyOutDir: true,
			outDir: 'node_modules/.mai-session-scope-fixture',
			rollupOptions: {
				input: SCOPE_ID,
				output: { entryFileNames: 'demo.js', assetFileNames: 'demo[extname]' }
			}
		}
	});
	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') harnessJs += item.code;
			else if (item.fileName.endsWith('.css')) harnessCss += String(item.source);
		}
	}
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete');
	}
});

async function gotoHarness(page: Page): Promise<void> {
	const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${FIXTURE_URL}demo.css"></head>
<body style="margin:0"><div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script></body></html>`;
	await page.route('**/*', (route: Route) => {
		const requestUrl = new URL(route.request().url());
		if (requestUrl.port !== '4173') return route.continue();
		if (requestUrl.pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (requestUrl.pathname.endsWith('demo.css')) {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
	});
	await page.goto(FIXTURE_URL);
}

const SERVICES = [
	{ application_id: SERVICE_ID, name: 'nginx-proxy', category: 'web' },
	{
		application_id: 'aaaa1111-0000-4000-8000-00000000000a',
		name: 'grafana',
		category: 'observability'
	}
];

test.describe('header chips render the stored session scope', () => {
	test('General Consult chip without a service chip', async ({ page }) => {
		await gotoHarness(page);
		await page.evaluate((serverId) => {
			return window.__mount!({
				sessionId: '11111111-1111-4111-8111-111111111111',
				serverId,
				detail: { server_id: serverId, service_id: null, service_name: null }
			});
		}, GLOBAL_SERVER_ID);
		await expect(page.getByTestId('session-header-server')).toHaveText('General Consult');
		await expect(page.getByTestId('session-header-service')).toHaveCount(0);
	});

	test('server label + linked service name render from the stored header', async ({
		page
	}) => {
		await gotoHarness(page);
		await page.evaluate(({ serverId, serviceId }) => {
			return window.__mount!({
				sessionId: '11111111-1111-4111-8111-111111111111',
				serverId,
				detail: {
					server_id: serverId,
					service_id: serviceId,
					service_name: 'nginx-proxy'
				},
				services: {
					serverOptions: [{ id: serverId, label: 'alpha-host' }]
				}
			});
		}, { serverId: SERVER_A_ID, serviceId: SERVICE_ID });
		await expect(page.getByTestId('session-header-server')).toHaveText('Server: alpha-host');
		await expect(page.getByTestId('session-header-service')).toHaveText('Service: @nginx-proxy');
	});

	test('the chips use the SESSION stored server, not the drawer selection', async ({
		page
	}) => {
		await gotoHarness(page);
		// The drawer selection is server B; the session's stored server is A.
		await page.evaluate((storedServerId) => {
			return window.__mount!({
				sessionId: '11111111-1111-4111-8111-111111111111',
				serverId: '9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d',
				detail: { server_id: storedServerId, service_id: null, service_name: null },
				services: { serverOptions: [{ id: storedServerId, label: 'alpha-host' }] }
			});
		}, SERVER_A_ID);
		await expect(page.getByTestId('session-header-server')).toHaveText('Server: alpha-host');
	});

	test('unknown enrolled server renders the honest short UUID', async ({ page }) => {
		await gotoHarness(page);
		await page.evaluate((serverId) => {
			return window.__mount!({
				sessionId: '11111111-1111-4111-8111-111111111111',
				serverId,
				detail: { server_id: serverId, service_id: null, service_name: null },
				services: { serverOptions: [] }
			});
		}, SERVER_A_ID);
		await expect(page.getByTestId('session-header-server')).toHaveText(
			`Server: ${SERVER_A_ID.slice(0, 8)}…`
		);
	});

	test('deleted map entry renders the explicit unavailable fallback', async ({ page }) => {
		await gotoHarness(page);
		await page.evaluate(({ serverId, serviceId }) => {
			return window.__mount!({
				sessionId: '11111111-1111-4111-8111-111111111111',
				serverId,
				detail: {
					server_id: serverId,
					service_id: serviceId,
					service_name: null
				}
			});
		}, { serverId: SERVER_A_ID, serviceId: SERVICE_ID });
		await expect(page.getByTestId('session-header-service')).toHaveText(
			`Service: @${SERVICE_ID.slice(0, 8)}… (unavailable)`
		);
	});

	test('a failed detail read renders the honest unavailable state', async ({ page }) => {
		await gotoHarness(page);
		await page.evaluate((serverId) => {
			return window.__mount!({
				sessionId: '11111111-1111-4111-8111-111111111111',
				serverId,
				detail: null,
				detailError: true
			});
		}, SERVER_A_ID);
		await expect(page.getByTestId('session-header-server')).toHaveText('Server unavailable');
	});
});

test.describe('composer @service picker (new sessions only)', () => {
	test('loading state renders while the slice loads', async ({ page }) => {
		await gotoHarness(page);
		await page.evaluate(() => window.__mount!({ services: { servicesLoading: true } }));
		await expect(page.getByTestId('session-services-loading')).toBeVisible();
		await expect(page.getByTestId('session-service-select')).toHaveCount(0);
	});

	test('error state renders the honest slice failure', async ({ page }) => {
		await gotoHarness(page);
		await page.evaluate(() =>
			window.__mount!({ services: { servicesError: 'map unreadable' } })
		);
		await expect(page.getByTestId('session-services-error')).toHaveText(
			'Services unavailable: map unreadable'
		);
	});

	test('empty state renders without a select', async ({ page }) => {
		await gotoHarness(page);
		await page.evaluate(() => window.__mount!({ services: { availableServices: [] } }));
		await expect(page.getByTestId('session-services-empty')).toBeVisible();
		await expect(page.getByTestId('session-service-select')).toHaveCount(0);
	});

	test('selection commits the canonical application_id at CREATE', async ({ page }) => {
		await gotoHarness(page);
		await page.evaluate(({ serverId, serviceId }) => {
			return window.__mount!({
				serverId,
				services: {
					availableServices: [
						{ application_id: serviceId, name: 'nginx-proxy', category: 'web' }
					],
					servicesHasMore: false
				}
			});
		}, { serverId: SERVER_A_ID, serviceId: SERVICE_ID });
		const select = page.getByTestId('session-service-select');
		await expect(select).toBeVisible();
		await select.selectOption(SERVICE_ID);
		// "Load more" only renders when has_more (bounded continuation).
		await expect(page.getByTestId('session-services-load-more')).toHaveCount(0);
		await page.getByTestId('session-composer-input').fill('check the proxy');
		await page.getByTestId('session-composer-submit').click();
		await expect
			.poll(() =>
				page.evaluate(() => window.__scope!.createLog.map((row) => row.service_id))
			)
			.toEqual([SERVICE_ID]);
		await expect
			.poll(() =>
				page.evaluate(() => window.__scope!.createLog.map((row) => row.server_id))
			)
			.toEqual([SERVER_A_ID]);
		// The committed session closes the draft picker (no silent rebind).
		await expect(page.getByTestId('session-service-select')).toHaveCount(0);
	});

	test('an existing session never shows the picker (read-only chip only)', async ({
		page
	}) => {
		await gotoHarness(page);
		await page.evaluate(({ serverId, serviceId }) => {
			return window.__mount!({
				sessionId: '11111111-1111-4111-8111-111111111111',
				serverId,
				detail: {
					server_id: serverId,
					service_id: serviceId,
					service_name: 'nginx-proxy'
				},
				services: { availableServices: [{ application_id: serviceId, name: 'nginx-proxy' }] }
			});
		}, { serverId: SERVER_A_ID, serviceId: SERVICE_ID });
		await expect(page.getByTestId('session-service-picker')).toHaveCount(0);
		await expect(page.getByTestId('session-header-service')).toHaveText('Service: @nginx-proxy');
	});

	test('load more continues the bounded page through the bag', async ({ page }) => {
		await gotoHarness(page);
		await page.evaluate((services) => {
			return window.__mount!({
				services: {
					availableServices: services,
					servicesHasMore: true,
					loadMoreServices: () => {
						window.__scope!.loadMoreLog.push(1);
					}
				}
			});
		}, SERVICES);
		await page.getByTestId('session-services-load-more').click();
		await expect
			.poll(() => page.evaluate(() => window.__scope!.loadMoreLog.length))
			.toBe(1);
	});
});

test.describe('360x640 bounds', () => {
	test('long service names wrap inside the chip with zero horizontal overflow', async ({
		page
	}) => {
		await page.setViewportSize({ width: 360, height: 640 });
		await gotoHarness(page);
		const longName = 'x'.repeat(160);
		await page.evaluate(({ serverId, serviceId, name }) => {
			return window.__mount!({
				sessionId: '11111111-1111-4111-8111-111111111111',
				serverId,
				detail: {
					server_id: serverId,
					service_id: serviceId,
					service_name: name
				},
				services: {
					serverOptions: [
						{ id: serverId, label: 'a-very-long-server-label-'.repeat(3) }
					]
				}
			});
		}, { serverId: SERVER_A_ID, serviceId: SERVICE_ID, name: longName });
		await expect(page.getByTestId('session-header-service')).toContainText(
			`@${longName.slice(0, 20)}`
		);
		const overflow = await page.evaluate(() => ({
			scroll: document.documentElement.scrollWidth,
			client: document.documentElement.clientWidth
		}));
		expect(overflow.scroll).toBeLessThanOrEqual(overflow.client);
	});
});
