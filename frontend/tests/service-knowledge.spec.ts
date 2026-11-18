import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte as sveltePlugin } from '@sveltejs/vite-plugin-svelte';
import path from 'node:path';

const FRONTEND_ROOT = path.resolve().concat(path.sep);

declare global {
	interface Window {
		__maiState: {
			defer: boolean;
			errorSection: string;
			noknowledge: boolean;
			empty: boolean;
			retractConflict: boolean;
			retryConflict: boolean;
		};
		__maiCalls: Array<{
			op: string;
			serverId: string;
			applicationId: string;
			page?: { limit?: number; offset?: number };
			payload?: { expected_revision: number; expected_generation: number; reason?: string };
			operationId?: string;
		}>;
		__maiPending: Array<{
			scope: string;
			op: string;
			resolve: (document: unknown) => void;
		}>;
		__maiReleaseFirst(): number;
		__maiReleaseAll(): number;
		__maiSvkMount(scopeApplicationId: string): unknown;
		__stale: Array<{
			scope: string;
			op: string;
			resolve: (document: unknown) => void;
		}>;
		__maiKnowledgeDoc(scope: string): unknown;
	}
}
const VIRTUAL_ID = 'virtual:service-knowledge-fixture';
const FIXTURE_URL = '/__mai_service_knowledge_fixture/';

const SERVER_A = '5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60';
const APP_ONE = '0aaaaaaa-0000-4000-8000-000000000001';
const APP_TWO = '0bbbbbbb-0000-4000-8000-000000000002';
const VIEWPORT = { width: 360, height: 640 };

/** The in-page harness entry: mounts the REAL component with a recording
 * mock whose behavior the tests drive through window.__maiState / the
 * pending-response gate. Query parameters seed the initial behavior so
 * each test opens the fixture in its needed mode. */
function harnessEntry(): string {
	return `
	import { mount } from 'svelte';
	import ServiceKnowledge from '$lib/components/ServiceKnowledge.svelte';

	const query = new URLSearchParams(location.search);
	const serverId = query.get('server') ?? '${SERVER_A}';
	const applicationId = query.get('app') ?? '${APP_ONE}';

	const state = {
		defer: query.get('defer') === '1',
		errorSection: query.get('errorSection') ?? '',
		noknowledge: query.get('noknowledge') === '1',
		empty: query.get('empty') === '1',
		retractConflict: false,
		retryConflict: false
	};
	window.__maiState = state;
	window.__maiCalls = [];
	window.__maiPending = [];

	function rowsFor(scope, page) {
		const limit = page?.limit ?? 2;
		const offset = page?.offset ?? 0;
		const total = state.empty ? 0 : 3;
		const items = [];
		for (let index = offset; index < Math.min(offset + limit, total); index += 1) {
			items.push({
				fact_id: 'f-' + scope + '-' + index,
				category: 'deployment',
				key: 'k' + index,
				statement: 'scope ' + scope + ' fact ' + index,
				revision: index + 1,
				status: 'active',
				source: { source_kind: 'tool_result', source_ref: 'ref-' + index, verified_at: '2026-01-01T00:00:00Z' }
			});
		}
		return { items, limit, offset, total, has_more: offset + limit < total };
	}

	function historyFor(scope, page) {
		const limit = page?.limit ?? 2;
		const offset = page?.offset ?? 0;
		const total = state.empty ? 0 : 3;
		const items = [];
		for (let index = offset; index < Math.min(offset + limit, total); index += 1) {
			items.push({
				revision_id: 'r-' + scope + '-' + index,
				fact_id: 'f-' + scope + '-' + index,
				revision: index + 1,
				statement: 'scope ' + scope + ' fact ' + index,
				status: index === 0 ? 'active' : 'superseded'
			});
		}
		return { items, limit, offset, total, has_more: offset + limit < total };
	}

	function operationsFor(scope, page) {
		const limit = page?.limit ?? 2;
		const offset = page?.offset ?? 0;
		const items = [];
		if (!state.empty && offset < 1) {
			items.push({
				operation_id: 'op-' + scope,
				status: 'queued',
				workflow_id: 'service.learn:op-' + scope,
				accepted_at: '2026-01-01T00:00:00Z'
			});
		}
		return { items, limit, offset, total: items.length, has_more: false };
	}

	function failSection(section) {
		const error = new Error(section + ' read failed');
		error.code = 'internal_error';
		throw error;
	}

	const knowledge = {
		async readKnowledge(serverId, applicationId, page) {
			window.__maiCalls.push({ op: 'readKnowledge', serverId, applicationId, page });
			if (state.errorSection === 'facts') failSection('facts');
			if (state.defer) {
				const deferred = Promise.withResolvers();
				window.__maiPending.push({ scope: applicationId, op: 'readKnowledge', resolve: deferred.resolve });
				return deferred.promise;
			}
			return {
				facts: rowsFor(applicationId, page),
				generation: 1,
				server_id: serverId,
				application_id: applicationId
			};
		},
		async readFactHistory(serverId, applicationId, factId, page) {
			window.__maiCalls.push({ op: 'readFactHistory', serverId, applicationId, page });
			if (state.errorSection === 'history') failSection('history');
			if (state.defer) {
				const deferred = Promise.withResolvers();
				window.__maiPending.push({ scope: applicationId, op: 'readFactHistory', resolve: deferred.resolve });
				return deferred.promise;
			}
			return historyFor(applicationId, page);
		},
		async readKnowledgeOperations(serverId, applicationId, status, page) {
			window.__maiCalls.push({ op: 'readKnowledgeOperations', serverId, applicationId, page });
			if (state.errorSection === 'operations') failSection('operations');
			if (state.defer) {
				const deferred = Promise.withResolvers();
				window.__maiPending.push({ scope: applicationId, op: 'readKnowledgeOperations', resolve: deferred.resolve });
				return deferred.promise;
			}
			return operationsFor(applicationId, page);
		},
		async retractKnowledgeFact(serverId, applicationId, factId, payload) {
			window.__maiCalls.push({ op: 'retractKnowledgeFact', serverId, applicationId, payload });
			if (state.retractConflict) {
				return {
					server_id: serverId,
					application_id: applicationId,
					generation: 1,
					fact_id: factId,
					new_revision: 0,
					status: 'failed',
					error_code: 'revision_conflict',
					error_detail: 'expected revision does not match the stored revision'
				};
			}
			return {
				server_id: serverId,
				application_id: applicationId,
				generation: 1,
				fact_id: factId,
				new_revision: payload.expected_revision + 1,
				status: 'applied'
			};
		},
		async retryKnowledgeOperation(serverId, applicationId, operationId) {
			window.__maiCalls.push({ op: 'retryKnowledgeOperation', serverId, applicationId, operationId });
			if (state.retryConflict) {
				const error = new Error('only queued operations are retryable');
				error.code = 'operation_not_runnable';
				throw error;
			}
			return {
				status: 'accepted',
				operation_id: operationId,
				workflow_id: 'service.learn:' + operationId,
				receipt_status: 'accepted'
			};
		}
	};

	window.__maiKnowledgeDoc = (scope) => ({
		facts: rowsFor(scope, { limit: 2, offset: 0 }),
		generation: 1,
		server_id: '${SERVER_A}',
		application_id: scope
	});
	window.__maiReleaseFirst = () => {
		const pending = window.__maiPending.shift();
		if (!pending) return 0;
		if (pending.op === 'readKnowledge') {
			pending.resolve({
				facts: rowsFor(pending.scope, { limit: 2, offset: 0 }),
				generation: 1,
				server_id: '${SERVER_A}',
				application_id: pending.scope
			});
		} else if (pending.op === 'readFactHistory') {
			pending.resolve(historyFor(pending.scope, { limit: 2, offset: 0 }));
		} else {
			pending.resolve(operationsFor(pending.scope, { limit: 2, offset: 0 }));
		}
		return window.__maiPending.length;
	};
	window.__maiReleaseAll = () => {
		while (window.__maiPending.length > 0) window.__maiReleaseFirst();
		return window.__maiPending.length;
	};

	function mountFresh(scopeApplicationId) {
		window.__maiCalls = [];
		window.__maiPending = [];
		// A remount must REPLACE the previous instance: Svelte's mount()
		// appends to the target, and two live components would answer
		// every testid query twice.
		document.getElementById('app').replaceChildren();
		return mount(ServiceKnowledge, {
			target: document.getElementById('app'),
			props: {
				serverId,
				applicationId: scopeApplicationId,
				knowledge: state.noknowledge ? null : knowledge,
				pageLimit: 2,
				reasonMaxLength: 400
			}
		});
	}

	window.__maiSvkMount = mountFresh;
	mountFresh(applicationId);
	`;
}

let harnessJs = '';
let harnessCss = '';

function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-service-knowledge-harness',
		resolveId(id: string) {
			if (id === VIRTUAL_ID) return `\0${VIRTUAL_ID}`;
			return undefined;
		},
		load(id: string) {
			if (id === `\0${VIRTUAL_ID}`) return code;
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [sveltePlugin({ inspector: false }), harnessPlugin(harnessEntry())],
		resolve: {
			alias: {
				// The component tree uses the SvelteKit `$lib` alias; the plain
				// in-memory build maps it to the same directory.
				$lib: `${FRONTEND_ROOT}src/lib`
			}
		},
		build: {
			write: false,
			emptyOutDir: true,
			outDir: 'node_modules/.mai-service-knowledge-fixture',
			rollupOptions: {
				input: VIRTUAL_ID,
				output: { entryFileNames: 'demo.js', assetFileNames: 'demo[extname]' }
			}
		}
	});

	const built = result as {
		output: Array<
			| { type: 'chunk'; code: string }
			| { type: 'asset'; fileName: string; source: string }
		>;
	} | {
		output: Array<
			| { type: 'chunk'; code: string }
			| { type: 'asset'; fileName: string; source: string }
		>;
	}[];
	const outputs = Array.isArray(built) ? built : [built];
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') harnessJs += item.code;
			else if ('fileName' in item && item.fileName.endsWith('.css')) {
				harnessCss += String(item.source);
			}
		}
	}
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete: missing JS chunk or CSS asset');
	}
});

async function openFixture(page: Page, query = ''): Promise<void> {
	await page.setViewportSize(VIEWPORT);
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAI service-knowledge fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body style="padding-top:8px">
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script>
</body>
</html>`;
	await page.route('**/*', (route: Route) => {
		const requestUrl = new URL(route.request().url());
		if (!requestUrl.pathname.startsWith(FIXTURE_URL)) {
			return route.fulfill({ contentType: 'text/html', body: html });
		}
		if (requestUrl.pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (requestUrl.pathname.endsWith('demo.css')) {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		return route.fulfill({ contentType: 'text/html', body: html });
	});
	await page.goto(`${FIXTURE_URL}${query}`);
}

async function expectReady(page: Page): Promise<void> {
	await expect(page.getByTestId('knowledge-status')).toContainText('Ready');
}

test.describe('service-knowledge bounded notes (360x640, keyboard)', () => {
	test.describe.configure({ retries: 0 });
	test.setTimeout(90_000);

	test('renders ready with bounded scrollable sections, inert text and source attribution', async ({
		page
	}) => {
		await openFixture(page);
		await expectReady(page);

		for (const section of ['knowledge-facts', 'knowledge-history', 'knowledge-operations']) {
			const region = page.getByTestId(section);
			await expect(region).toBeVisible();
			const styles = await region.evaluate((element) => {
				const computed = getComputedStyle(element);
				return { overflowY: computed.overflowY, maxHeight: computed.maxHeight };
			});
			expect(styles.overflowY).toBe('auto');
			const maxHeight = parseInt(styles.maxHeight, 10);
			expect(Number.isNaN(maxHeight)).toBe(false);
			expect(maxHeight).toBeGreaterThan(0);
		}

		// Inert text: statements are escaped text nodes, never markup.
		const factRow = page.getByTestId('knowledge-fact-row').first();
		await expect(factRow).toContainText('scope');
		expect(await factRow.locator('script, img, iframe').count()).toBe(0);
		// Accessible source attribution: provenance kind + reference ride the
		// row's text.
		await expect(factRow).toContainText('source: tool_result');
		await expect(factRow).toContainText('ref-0');
	});

	test('scope epoch guard discards stale responses from a replaced application', async ({
		page
	}) => {
		await openFixture(page, '?defer=1');
		// The old scope's reads are parked on the deferred gate.

		// Park the OLD scope's pending resolvers aside, then REPLACE the
		// component with the new scope (fresh pending reads).
		await page.evaluate(() => {
			window.__stale = window.__maiPending.splice(0);
			window.__maiSvkMount('0bbbbbbb-0000-4000-8000-000000000002');
		});
		const parked = await page.evaluate(() => ({
			staleCount: window.__stale.length,
			pendingCount: window.__maiPending.length
		}));
		expect(parked.staleCount).toBe(3);
		expect(parked.pendingCount).toBe(3);

		// The STALE responses resolve into the DETACHED old component:
		// they can never paint the new scope (epoch guard discards them).
		await page.evaluate(() => {
			for (const pending of window.__stale) {
				if (pending.op === 'readKnowledge') {
					pending.resolve(window.__maiKnowledgeDoc(pending.scope));
				}
			}
		});
		await expect(page.getByTestId('knowledge-fact-row').first()).toHaveCount(0);
		await expect(page.getByTestId('knowledge-status')).toContainText('Loading');

		// The NEW scope's responses land and paint the NEW scope only.
		await page.evaluate(() => {
			window.__maiReleaseAll();
		});
		await expectReady(page);
		const fact = page.getByTestId('knowledge-fact-row').first();
		await expect(fact).toContainText(`scope ${APP_TWO}`);
		const ids = await page
			.getByTestId('knowledge-fact-row')
			.evaluateAll((rows) => rows.map((row) => row.getAttribute('data-fact-id')));
		for (const id of ids) expect(id).toContain(APP_TWO);
	});

	test('load more pages with dedup via keyboard (Enter on the focused control)', async ({
		page
	}) => {
		await openFixture(page);
		await expectReady(page);
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(2);

		const loadMore = page.getByTestId('knowledge-facts-load-more');
		await expect(loadMore).toBeVisible();
		await loadMore.focus();
		await page.keyboard.press('Enter');

		// The overlapping page dedupes: 3 distinct rows, no duplicates.
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(3);
		const ids = await page
			.getByTestId('knowledge-fact-row')
			.evaluateAll((rows) => rows.map((row) => row.getAttribute('data-fact-id')));
		expect(new Set(ids).size).toBe(3);
		await expect(page.getByTestId('knowledge-facts-load-more')).toHaveCount(0);
	});

	test('loading, empty and fail-then-reload states are honest per section', async ({
		page
	}) => {
		await openFixture(page, '?defer=1');
		await expect(page.getByTestId('knowledge-status')).toContainText('Loading');
		await page.evaluate(() => {
			window.__maiState.defer = false;
			window.__maiReleaseAll();
		});
		await expectReady(page);

		// Error then explicit reload recovery on one section.
		await page.evaluate(() => {
			window.__maiState.errorSection = 'operations';
			window.__maiCalls = [];
			window.__maiSvkMount('0aaaaaaa-0000-4000-8000-000000000001');
		});
		await expect(page.getByTestId('knowledge-status')).toContainText(
			'A knowledge read failed'
		);
		await expect(page.getByTestId('knowledge-operations')).toContainText(
			'Operations read failed: internal_error'
		);
		await page.evaluate(() => {
			window.__maiState.errorSection = '';
			window.__maiCalls = [];
		});
		await page.getByTestId('knowledge-reload').click();
		await expectReady(page);
	});

	test('fail-closed: no knowledge capability group answers the unavailable note', async ({
		page
	}) => {
		await openFixture(page, '?noknowledge=1');
		await expect(page.getByTestId('knowledge-unavailable')).toBeVisible();
		await expect(page.getByTestId('knowledge-status')).toContainText(
			'Knowledge surface unavailable'
		);
	});

	test('empty collections answer the honest empty state', async ({ page }) => {
		await openFixture(page, '?empty=1');
		await expectReady(page);
		await expect(page.getByTestId('knowledge-facts-empty')).toBeVisible();
		await expect(page.getByTestId('knowledge-history-empty')).toBeVisible();
		await expect(page.getByTestId('knowledge-operations-empty')).toBeVisible();
	});

	test('retract sends the exact observed CAS and surfaces a conflict refusal', async ({
		page
	}) => {
		await openFixture(page);
		await expectReady(page);
		const reason = page.getByTestId('knowledge-reason');
		await reason.fill('operator verified the statement is wrong');

		await page.getByTestId('knowledge-fact-retract').first().click();
		await expect(page.getByTestId('knowledge-status')).toContainText('Ready');
		const calls = await page.evaluate(() => window.__maiCalls);
		const retract = calls.find((call) => call.op === 'retractKnowledgeFact');
		expect(retract).toBeDefined();
		expect(retract!.payload).toEqual({
			expected_revision: 1,
			expected_generation: 1,
			reason: 'operator verified the statement is wrong'
		});

		// Refusal: the conflict banner carries the stable refusal code.
		await page.evaluate(() => {
			window.__maiState.retractConflict = true;
		});
		await page.getByTestId('knowledge-fact-retract').first().click();
		await expect(page.getByTestId('knowledge-conflict')).toContainText(
			'revision_conflict'
		);
	});

	test('retry posts for queued operations and surfaces the refused retry as a conflict', async ({
		page
	}) => {
		await openFixture(page);
		await expectReady(page);
		const retry = page.getByTestId('knowledge-operation-retry').first();
		await expect(retry).toBeVisible();

		await retry.click();
		await expect(page.getByTestId('knowledge-status')).toContainText('Ready');
		const calls = await page.evaluate(() => window.__maiCalls);
		const retryCall = calls.find((call) => call.op === 'retryKnowledgeOperation');
		expect(retryCall).toBeDefined();
		expect(retryCall!.operationId).toContain(APP_ONE);

		// Refusal: non-runnable retry renders the bounded conflict note.
		await page.evaluate(() => {
			window.__maiState.retryConflict = true;
		});
		await page.getByTestId('knowledge-operation-retry').first().click();
		await expect(page.getByTestId('knowledge-conflict')).toContainText(
			'operation_not_runnable'
		);
	});
});
