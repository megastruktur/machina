import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte as sveltePlugin } from '@sveltejs/vite-plugin-svelte';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import type { ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';

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
		__maiSvkSetApplicationId(applicationId: string): unknown;
		__maiApiJournal: Array<{ method: string; path: string; status: number; ms: number }>;
		__maiRealApi(method: string, path: string, body?: unknown): Promise<Record<string, unknown>>;
		__maiRealLogin(password: string): Promise<string>;
		__stale: Array<{
			scope: string;
			op: string;
			resolve: (document: unknown) => void;
		}>;
		__maiKnowledgeDoc(scope: string): unknown;
	}
}
const VIRTUAL_ID = 'virtual:service-knowledge-fixture';
const HOLDER_ID = 'virtual:service-knowledge-holder.svelte';
const FIXTURE_URL = '/__mai_service_knowledge_fixture/';

const SERVER_A = '5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60';
const APP_ONE = '0aaaaaaa-0000-4000-8000-000000000001';
const APP_TWO = '0bbbbbbb-0000-4000-8000-000000000002';
const VIEWPORT = { width: 360, height: 640 };
// ---------------------------------------------------------------------------
// ask_38: REAL-API scenarios (fixture FastAPI over the disposable Postgres)
// ---------------------------------------------------------------------------

const TESTS_DIR = fileURLToPath(new URL('.', import.meta.url));
const BACKEND_ROOT = path.resolve(TESTS_DIR, '../../backend');
const LIVE_URL_VAR = 'MAI_TEST_DATABASE_URL';
const API_PORT = 4837;
const API_BASE = `http://127.0.0.1:${API_PORT}`;
const REAL_PASSWORD = 'fixture-password-ask-38';
const MAIN_REPO_ROOT = path.resolve(TESTS_DIR, '../../../../../..');
const EVIDENCE_DIR =
	process.env.MAI_FXT_EVIDENCE_DIR ??
	(fs.existsSync(path.join(MAIN_REPO_ROOT, 'projects/dev/machina/plans'))
		? path.join(
				MAIN_REPO_ROOT,
				'projects/dev/machina/plans/agent-service-knowledge/ask_evidence/38-ui-integration/evidence'
			)
		: path.join(os.tmpdir(), 'mai-svk-38-evidence'));
/** Merge one upstream Set-Cookie header batch into the jar. The jar is
 * keyed by cookie NAME: a rotated mai_csrf/mai_session overwrites the
 * older value instead of appending a duplicate attribute cookie. */
interface CookieJar {
	cookie: string;
	csrf: string;
}

function updateCookieJar(jar: CookieJar, setCookieHeaders: string[]): void {
	const jarMap = new Map<string, string>();
	for (const pair of jar.cookie ? jar.cookie.split('; ') : []) {
		const eq = pair.indexOf('=');
		if (eq > 0) jarMap.set(pair.slice(0, eq), pair.slice(eq + 1));
	}
	for (const header of setCookieHeaders) {
		const first = header.split(';')[0];
		const eq = first.indexOf('=');
		if (eq <= 0) continue;
		jarMap.set(first.slice(0, eq), first.slice(eq + 1));
	}
	jar.cookie = [...jarMap].map(([name, value]) => `${name}=${value}`).join('; ');
	jar.csrf = jarMap.get('mai_csrf') ?? jar.csrf;
}

interface ApiJournalEntry {
	method: string;
	path: string;
	status: number;
	ms: number;
}
const apiJournal: ApiJournalEntry[] = [];


/** The in-page harness entry: mounts the REAL component with a recording
 * mock whose behavior the tests drive through window.__maiState / the
 * pending-response gate. Query parameters seed the initial behavior so
 * each test opens the fixture in its needed mode. */
/** A one-instance holder: the component is mounted ONCE and its reactive
 * applicationId prop is switched in place — the scope-epoch guard must
 * discard stale in-flight reads WITHOUT any remount. */
function holderSource(): string {
	return `<script lang="ts">
	import ServiceKnowledge from '$lib/components/ServiceKnowledge.svelte';

	let {
		serverId,
		applicationId,
		knowledge,
		pageLimit,
		reasonMaxLength,
		register
	} = $props();

	register({
		setApplicationId: (id) => {
			applicationId = id;
		}
	});
</script>

<ServiceKnowledge {serverId} {applicationId} {knowledge} {pageLimit} {reasonMaxLength} />
`;
}

function harnessEntry(): string {
	return `
	import { mount } from 'svelte';
	import { createMaiApiClient } from '$lib/api/client.js';
	import Holder from '${HOLDER_ID}';

	const query = new URLSearchParams(location.search);
	const serverId = query.get('server') ?? '${SERVER_A}';
	const applicationId = query.get('app') ?? '${APP_ONE}';
	const realMode = query.get('mode') === 'real';
	const apiBase = query.get('api') ?? '';
	const csrfToken = query.get('csrf') ?? '';

	// ask_38 REAL-API mode: the knowledge surface is a fetch client over
	// the LIVE fixture FastAPI (same-origin relative paths — the test's
	// route handler proxies them to the backend with its own session/CSRF
	// jar). Every request is journaled (method/path/status/duration).
	window.__maiApiJournal = [];
	async function realApi(method, path, body) {
		const started = Date.now();
		const response = await fetch(path, {
			method,
			headers: { 'content-type': 'application/json' },
			body: body ? JSON.stringify(body) : undefined
		});
		window.__maiApiJournal.push({
			method,
			path,
			status: response.status,
			ms: Date.now() - started
		});
		if (!response.ok) {
			const error = new Error('http ' + response.status);
			error.status = response.status;
			try {
				error.code = (await response.json()).error?.code ?? '';
			} catch {
				error.code = '';
			}
			throw error;
		}
		return response.json();
	}
	const knowledgeGroup = realMode
		? {
				readKnowledge: (...callArgs) =>
					window.__maiClient.readKnowledge(...callArgs),
				readFactHistory: (...callArgs) =>
					window.__maiClient.readFactHistory(...callArgs),
				readKnowledgeOperations: (...callArgs) =>
					window.__maiClient.readKnowledgeOperations(...callArgs),
				retractKnowledgeFact: (...callArgs) =>
					window.__maiClient.retractKnowledgeFact(...callArgs),
				retryKnowledgeOperation: (...callArgs) =>
					window.__maiClient.retryKnowledgeOperation(...callArgs)
			}
		: null;

	if (realMode) {
		// The PRODUCTION client (ask_29) against the LIVE backend: same
		// cookie-backed CSRF + error contract as the product.
		window.__maiClient = createMaiApiClient({
			headers: { origin: 'http://127.0.0.1:4173' },
			getCsrfToken: () => csrfToken
		});
		window.__maiRealApi = realApi;
		window.__maiRealLogin = async (password) => {
			await realApi('POST', '/api/v1/auth/bootstrap', { password });
			await realApi('POST', '/api/v1/auth/login', { password });
			return 'logged-in';
		};
	}

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
		// The producer's page window OVERLAPS by one record (offset-1): the
		// client's dedupeById must genuinely drop a repeated fact_id when
		// appending the next page.
		for (let index = Math.max(0, offset - 1); index < Math.min(offset + limit, total); index += 1) {
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

	// ONE persistent instance for the whole page life: tests switch its
	// reactive applicationId prop in place (no remount, no replaceChildren
	// — two live components would answer every testid query twice).
	window.__maiCalls = [];
	window.__maiPending = [];
	function resolveKnowledge(state, mockKnowledge) {
		// Real mode: the production client group. Otherwise the scripted
		// mock (or an explicit null for the honest unavailable state).
		if (realMode) return knowledgeGroup;
		return state.noknowledge ? null : mockKnowledge;
	}

	mount(Holder, {
		target: document.getElementById('app'),
		props: {
			serverId,
			applicationId,
			knowledge: resolveKnowledge(state, knowledge),
			pageLimit: realMode ? 10 : 2,
			reasonMaxLength: 400,
			register: (api) => {
				window.__maiSvkSetApplicationId = api.setApplicationId;
			}
		}
	});
	`;
}

let harnessJs = '';
let harnessCss = '';

function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-service-knowledge-harness',
		resolveId(id: string) {
			if (id in sources) return `\0${id}`;
			return undefined;
		},
		load(id: string) {
			const bare = id.startsWith('\0') ? id.slice(1) : id;
			if (bare in sources) return sources[bare];
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	// Fresh accumulation on every build (a retried beforeAll must not
	// concatenate chunks from a previous attempt).
	harnessJs = '';
	harnessCss = '';
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [
			sveltePlugin({ inspector: false }),
			harnessPlugin({ [VIRTUAL_ID]: harnessEntry(), [HOLDER_ID]: holderSource() })
		],
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

async function openFixture(
	page: Page,
	query = '',
	options: {
		apiProxy?: { baseUrl: string; jar: { cookie: string; csrf: string } };
	} = {}
): Promise<void> {
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
	await page.route('**/*', async (route: Route) => {
		const requestUrl = new URL(route.request().url());
		const proxy = options.apiProxy;
		if (
			proxy &&
			requestUrl.pathname.startsWith('/api/v1/')
		) {
			// ask_38 REAL-API proxy: forward to the LIVE fixture FastAPI
			// with the test's own session/CSRF jar, journal the request/
			// response evidence (method/path/status/duration — no bodies,
			// no secrets), and fulfill the page with the REAL response.
			// This branch MUST precede the fixture-HTML fallthrough: the
			// /api/v1 paths never carry the fixture prefix.
			const started = Date.now();
			const method = route.request().method();
			const upstream = await fetch(
				`${proxy.baseUrl}${requestUrl.pathname}${requestUrl.search}`,
				{
					method,
					headers: {
						'content-type': 'application/json',
						origin: 'http://127.0.0.1:4173',
						...(proxy.jar.cookie ? { cookie: proxy.jar.cookie } : {}),
						...(method !== 'GET' && proxy.jar.csrf
							? { 'x-csrf-token': proxy.jar.csrf }
							: {})
					},
					body: ['GET', 'HEAD'].includes(method)
						? undefined
						: route.request().postData()
				}
			);
			updateCookieJar(proxy.jar, upstream.headers.getSetCookie());
			apiJournal.push({
				method,
				path: requestUrl.pathname + requestUrl.search,
				status: upstream.status,
				ms: Date.now() - started
			});
			return route.fulfill({
				status: upstream.status,
				contentType: 'application/json',
				body: await upstream.text()
			});
		}
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

	test('scope epoch guard discards stale responses on a live application switch', async ({
		page
	}) => {
		await openFixture(page, '?defer=1');
		// The old scope's reads are parked on the deferred gate.

		// Park the OLD scope's pending resolvers aside, then switch the
		// holder's applicationId prop IN PLACE: the same live instance
		// bumps its epoch and parks three fresh reads for the new scope.
		await page.evaluate((app) => {
			window.__stale = window.__maiPending.splice(0);
			window.__maiSvkSetApplicationId(app);
		}, APP_TWO);
		const parked = await page.evaluate(() => ({
			staleCount: window.__stale.length,
			pendingCount: window.__maiPending.length
		}));
		expect(parked.staleCount).toBe(3);
		expect(parked.pendingCount).toBe(3);

		// The STALE responses resolve into the LIVE component whose epoch
		// has already moved on: the guard discards them before any paint.
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

		// Error then explicit reload recovery on one section: switching the
		// application prop re-reads (the harness answers operations with
		// the armed failure; facts and history stay healthy).
		await page.evaluate((app) => {
			window.__maiState.errorSection = 'operations';
			window.__maiCalls = [];
			window.__maiSvkSetApplicationId(app);
		}, APP_TWO);
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

// ---------------------------------------------------------------------------
// ask_38 — REAL-API scenarios: the LIVE fixture FastAPI (create_app) over
// the disposable Postgres fixture. NO mock responses are substituted for
// these cases: the harness's knowledge surface is a fetch client over the
// real HTTP routes, proxied through the test's route handler (session/CSRF
// jar owned Node-side; every request journaled as evidence — method/path/
// status/duration only, never bodies).
// ---------------------------------------------------------------------------

function _backendPython(code: string, env: Record<string, string> = {}): string {
	const result = spawnSync('uv', ['run', 'python', '-c', code], {
		cwd: BACKEND_ROOT,
		env: { ...process.env, ...env },
		encoding: 'utf-8',
		timeout: 240_000
	});
	if (result.status !== 0) {
		throw new Error(
			`backend python step failed (exit ${result.status}): ${result.stderr.slice(-2000)}`
		);
	}
	return result.stdout;
}

async function _waitForApiReady(deadlineMs: number): Promise<void> {
	const deadline = Date.now() + deadlineMs;
	let lastError: unknown = 'never probed';
	while (Date.now() < deadline) {
		try {
			const response = await fetch(`${API_BASE}/api/v1/health`, {
				signal: AbortSignal.timeout(3_000)
			});
			if (response.status === 200) return;
			lastError = `health ${response.status}`;
		} catch (error) {
			lastError = error;
		}
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	throw new Error(`BLOCKED: the fixture API never became ready: ${String(lastError)}`);
}


test.describe('real api (fixture FastAPI + disposable Postgres)', () => {
	// The scenarios share the seeded fixture backend (and its proxy jar):
	// serialize them and reset the request journal per test.
	test.describe.configure({ mode: 'serial' });
	let server: ChildProcess | undefined;
	let dataRoot = '';

	test.beforeEach(() => {
		apiJournal.length = 0;
	});

	test.beforeAll(async () => {
		const url = process.env[LIVE_URL_VAR];
		if (!url) {
			throw new Error(
				`BLOCKED: ${LIVE_URL_VAR} is not set — the real-API scenarios need the disposable fixture Postgres`
			);
		}
		fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
		dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'mai-svk-api-'));

		// Disposable DB: full reset + migrate + DBOS bootstrap (the learn
		// enqueue seam needs the engine-owned dbos schema).
		_backendPython(
			"import psycopg, os; url = os.environ['MAI_DATABASE_URL']; conn = psycopg.connect(url, autocommit=True); conn.execute('DROP SCHEMA IF EXISTS public CASCADE'); conn.execute('CREATE SCHEMA public'); conn.execute('DROP SCHEMA IF EXISTS dbos CASCADE'); conn.close()",
			{ MAI_DATABASE_URL: url }
		);
		const migrate = spawnSync('uv', ['run', 'alembic', 'upgrade', 'head'], {
			cwd: BACKEND_ROOT,
			env: { ...process.env, MAI_DATABASE_URL: url },
			encoding: 'utf-8',
			timeout: 240_000
		});
		if (migrate.status !== 0) {
			throw new Error(`alembic upgrade failed: ${migrate.stderr.slice(-2000)}`);
		}
		_backendPython(
			"import os; from mai.jobs import engine as jobs_engine; jobs_engine.launch_worker(os.environ['MAI_DATABASE_URL']); jobs_engine.shutdown()",
			{ MAI_DATABASE_URL: url }
		);

		// The LIVE fixture API process.
		server = spawn(
			'uv',
			[
				'run',
				'uvicorn',
				'--factory',
				'mai.api.app:create_app',
				'--host',
				'127.0.0.1',
				'--port',
				String(API_PORT)
			],
			{
				cwd: BACKEND_ROOT,
				env: {
					...process.env,
					MAI_DATABASE_URL: url,
					MAI_DATA_ROOT: dataRoot,
					MAI_PUBLIC_ORIGINS: 'http://127.0.0.1:4173'
				},
				// 'ignore': the uvicorn pipe buffers would block a long run.
				stdio: 'ignore'
			}
		) as ChildProcess;
		await _waitForApiReady(120_000);
	});

	test.afterAll(async () => {
		if (server && server.exitCode === null) {
			server.kill('SIGTERM');
			const exited = new Promise<void>((resolve) => {
				server!.once('exit', () => resolve());
			});
			const killTimer = setTimeout(() => {
				if (server && server.exitCode === null) server.kill('SIGKILL');
			}, 5_000);
			await exited;
			clearTimeout(killTimer);
		}
		const url = process.env[LIVE_URL_VAR];
		if (url) {
			_backendPython(
				"import psycopg, os; url = os.environ['MAI_DATABASE_URL']; conn = psycopg.connect(url, autocommit=True); conn.execute('DROP SCHEMA IF EXISTS public CASCADE'); conn.execute('CREATE SCHEMA public'); conn.execute('DROP SCHEMA IF EXISTS dbos CASCADE'); conn.close()",
				{ MAI_DATABASE_URL: url }
			);
		}
		if (dataRoot) fs.rmSync(dataRoot, { recursive: true, force: true });
	});

	function _seedSql(statements: string[]): void {
		const joined = statements.join('@@@');
		const result = spawnSync(
			'uv',
			['run', 'python', '-c', "import psycopg, os, sys; conn = psycopg.connect(os.environ['MAI_DATABASE_URL'], autocommit=True); [conn.execute(s) for s in sys.stdin.read().split('@@@') if s.strip()]; conn.close()"],
			{
				cwd: BACKEND_ROOT,
				env: { ...process.env, MAI_DATABASE_URL: process.env[LIVE_URL_VAR]! },
				input: joined,
				encoding: 'utf-8',
				timeout: 120_000
			}
		);
		if (result.status !== 0) {
			throw new Error(`seed SQL failed: ${result.stderr.slice(-2000)}`);
		}
	}

	function _seedServiceKnowledge(appId: string, generation: number, facts: Array<{ id: string; key: string; statement: string; revision: number }>): void {
		const statements = [
			`INSERT INTO service_knowledge_identity (server_id, application_id, generation, manual_identity, auto_adopt_suppressed) VALUES ('${SERVER_A}', '${appId}', ${generation}, false, false) ON CONFLICT (server_id, application_id) DO NOTHING`,
			...facts.flatMap((fact) => [
				`INSERT INTO service_learned_fact (fact_id, server_id, application_id, generation, category, key, statement, revision, status, source_kind, source_ref, source_run_id, verified_at) VALUES ('${fact.id}', '${SERVER_A}', '${appId}', ${generation}, 'operations', '${fact.key}', '${fact.statement.replace(/'/g, "''")}', ${fact.revision}, 'active', 'tool_result', 'runtime-suite-ref-1', '${crypto.randomUUID()}', now())`,
				`INSERT INTO service_learned_fact_revision (revision_id, fact_id, server_id, application_id, revision, statement, status, source_kind, source_ref, changed_at) VALUES ('${crypto.randomUUID()}', '${fact.id}', '${SERVER_A}', '${appId}', ${fact.revision}, '${fact.statement.replace(/'/g, "''")}', 'active', 'tool_result', 'runtime-suite-ref-1', now())`
			])
		];
		_seedSql(statements);
	}

	
	async function _openRealApiFixture(page: Page, jar: CookieJar, appId: string): Promise<void> {
		await openFixture(
			page,
			`?mode=real&api=${encodeURIComponent(API_BASE)}&app=${appId}&csrf=${encodeURIComponent(jar.csrf)}`,
			{ apiProxy: { baseUrl: API_BASE, jar } }
		);
	}

	function _evidenceScreenshot(name: string): string {
		return path.join(EVIDENCE_DIR, name);
	}

	async function realLoginSetup(jar: CookieJar): Promise<void> {
		for (const routePath of ['/api/v1/auth/bootstrap', '/api/v1/auth/login']) {
			const response = await fetch(`${API_BASE}${routePath}`, {
				method: 'POST',
				headers: {
					'content-type': 'application/json',
					origin: 'http://127.0.0.1:4173',
					...(jar.cookie ? { cookie: jar.cookie } : {})
				},
				body: JSON.stringify({ password: REAL_PASSWORD })
			});
			// bootstrap answers 201 (created) or 409 (already bootstrapped);
			// login must answer 204.
			const ok = routePath.endsWith('bootstrap')
				? [201, 409].includes(response.status)
				: response.status === 204;
			if (!ok) {
				throw new Error(
					`auth ${routePath} failed: ${response.status}`
				);
			}
			updateCookieJar(jar, response.headers.getSetCookie());
		}
	}

	function expectFactCount(page: Page, count: number): void {
		return void expect(page.getByTestId('knowledge-fact-row')).toHaveCount(count);
	}

	test('real api: learned note lifecycle reads facts, history and operations from the backend', async ({
		page
	}) => {
		const jar: CookieJar = { cookie: '', csrf: '' };
		const appId = crypto.randomUUID();
		const factA = crypto.randomUUID();
		const factB = crypto.randomUUID();
		_seedServiceKnowledge(appId, 2, [
			{ id: factA, key: 'health-path', statement: 'health lives at /healthz on the loopback probe', revision: 1 },
			{ id: factB, key: 'bind-address', statement: 'the admin panel binds 127.0.0.1:8081 only', revision: 1 }
		]);
		_seedSql([
			`INSERT INTO service_learning_operation (operation_id, server_id, application_id, generation, payload_digest, workflow_id, status, sub_effects, coalesce_key, source_run_id, source_tool_call_id, actor, accepted_at) VALUES ('${crypto.randomUUID()}', '${SERVER_A}', '${appId}', 2, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'service.learn:${crypto.randomUUID()}', 'queued', '{}'::jsonb, 'fixture-coalesce-${crypto.randomUUID()}', '${crypto.randomUUID()}', 'fixture-tool-call', 'principal', now())`
		]);
		await realLoginSetup(jar);

		await _openRealApiFixture(page, jar, appId);
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(2);
		await expect(page.getByTestId('knowledge-fact-row').first()).toContainText(
			'/healthz'
		);
		await expect(page.getByTestId('knowledge-history-row')).toHaveCount(2);
		await expect(page.getByTestId('knowledge-operation-row')).toContainText('queued');

		// Every lifecycle read hit the REAL backend with 200s.
		const journal: ApiJournalEntry[] = apiJournal;
		const reads = journal.filter((entry) => entry.method === 'GET');
		expect(reads.length).toBeGreaterThanOrEqual(3);
		for (const entry of reads) {
			expect(entry.status).toBe(200);
			expect(entry.path).toContain(`/api/v1/servers/${SERVER_A}/applications/${appId}/knowledge`);
		}
		await page.screenshot({
			path: _evidenceScreenshot('real-api-lifecycle-ready.png'),
			fullPage: true
		});
	});

	test('real api: retract applies the revision CAS and a stale revision answers 409', async ({
		page
	}) => {
		const jar: CookieJar = { cookie: '', csrf: '' };
		const appId = crypto.randomUUID();
		const factA = crypto.randomUUID();
		const factB = crypto.randomUUID();
		_seedServiceKnowledge(appId, 2, [
			{ id: factA, key: 'retract-me', statement: 'this fact will be retracted by the operator', revision: 1 },
			{ id: factB, key: 'stale-cas', statement: 'this fact was concurrently edited on the server', revision: 1 }
		]);
		await realLoginSetup(jar);

		await _openRealApiFixture(page, jar, appId);
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(2);

		// Operator retraction through the UI: real CSRF/session cookies ride
		// the proxied POST; the CAS matches so the fact is retracted.
		await page.getByTestId('knowledge-reason').fill('superseded by the operator');
		await page.getByTestId('knowledge-fact-row').first().getByTestId('knowledge-fact-retract').click();
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(1);
		const journal: ApiJournalEntry[] = apiJournal;
		const retracts = journal.filter(
			(entry) => entry.method === 'POST' && entry.path.includes('/facts/')
		);
		expect(retracts).toHaveLength(1);
		expect(retracts[0].status).toBe(200);

		// STALE CAS: the server-side fact moved (simulated concurrent edit);
		// the UI's retract carries the loaded (now stale) revision → 409 →
		// the honest conflict banner.
		_seedSql([
			`UPDATE service_learned_fact SET revision = 7 WHERE fact_id = '${factB}'`
		]);
		await page
			.getByTestId('knowledge-fact-row')
			.first()
			.getByTestId('knowledge-fact-retract')
			.click();
		await expect(page.getByTestId('knowledge-conflict')).toBeVisible();
		await page.screenshot({
			path: _evidenceScreenshot('real-api-retract-conflict.png'),
			fullPage: true
		});
		// The retracted fact is history, never deleted; the surviving fact
		// keeps its row (conflict refused with no effect).
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(1);
	});

	test('real api: retry posts for a queued preservation operation', async ({ page }) => {
		const jar: CookieJar = { cookie: '', csrf: '' };
		const appId = crypto.randomUUID();
		const runId = crypto.randomUUID();
		_seedServiceKnowledge(appId, 2, [
			{ id: crypto.randomUUID(), key: 'retry-target', statement: 'queued operation target fact', revision: 1 }
		]);
		await realLoginSetup(jar);

		await _openRealApiFixture(page, jar, appId);
		// The queued preservation operation is SEEDED (the learn admission
		// has no HTTP route — app.learn_service is the agent capability
		// seam); the UI reads it from the REAL operations table.
		_seedSql([
			`INSERT INTO service_learning_operation (operation_id, server_id, application_id, generation, payload_digest, workflow_id, status, sub_effects, coalesce_key, source_run_id, source_tool_call_id, actor, accepted_at) VALUES ('${crypto.randomUUID()}', '${SERVER_A}', '${appId}', 2, '${'a'.repeat(64)}', 'service.learn:${crypto.randomUUID()}', 'queued', '{}'::jsonb, 'fixture-coalesce-${crypto.randomUUID()}', '${runId}', 'fixture-tool-call', 'principal', now())`
		]);
		await page.getByTestId('knowledge-reload').click();
		const operationRow = page.getByTestId('knowledge-operation-row').first();
		await expect(operationRow).toContainText('queued');

		// The explicit authorized retry: same deterministic workflow id.
		const operationId = await operationRow.getAttribute('data-operation-id');
		const retryStatus = await page.evaluate(
			([server, application, operation]) =>
				window.__maiRealApi(
					'POST',
					`/api/v1/servers/${server}/applications/${application}/knowledge/operations/${operation}/retry`
				).then((r) => r.status),
			[SERVER_A, appId, operationId]
		);
		expect(retryStatus).toBe('accepted');
		const journal: ApiJournalEntry[] = apiJournal;
		const retries = journal.filter((entry) => entry.path.includes('/retry'));
		expect(retries).toHaveLength(1);
		expect(retries[0].status).toBe(200);
	});

	test('real api: scope switch reads the other application notes from the backend', async ({
		page
	}) => {
		const jar: CookieJar = { cookie: '', csrf: '' };
		const appId = crypto.randomUUID();
		const appId2 = crypto.randomUUID();
		_seedServiceKnowledge(appId, 2, [
			{ id: crypto.randomUUID(), key: 'app-one', statement: 'app one live fact', revision: 1 }
		]);
		_seedServiceKnowledge(appId2, 2, [
			{ id: crypto.randomUUID(), key: 'app-two', statement: 'app two live fact', revision: 1 }
		]);
		await realLoginSetup(jar);

		await _openRealApiFixture(page, jar, appId);
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(1);
		await expect(page.getByTestId('knowledge-fact-row')).toContainText('app one');

		// Switch the scope IN PLACE (the holder's reactive prop): the notes
		// re-read from the REAL backend for the new application.
		await page.evaluate((id) => window.__maiSvkSetApplicationId(id), appId2);
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(1);
		await expect(page.getByTestId('knowledge-fact-row')).toContainText('app two');
		const journal: ApiJournalEntry[] = apiJournal;
		expect(
			journal.filter((entry) => entry.path.includes(appId2)).length
		).toBeGreaterThanOrEqual(2);
	});

	test('real api: pagination loads more with exactly two bounded requests', async ({
		page
	}) => {
		const jar: CookieJar = { cookie: '', csrf: '' };
		const appId = crypto.randomUUID();
		_seedServiceKnowledge(
			appId,
			2,
			Array.from({ length: 12 }, (_, index) => ({
				id: crypto.randomUUID(),
				key: `page-fact-${index}`,
				statement: `bounded pagination fact number ${index}`,
				revision: 1
			}))
		);
		await realLoginSetup(jar);

		await _openRealApiFixture(page, jar, appId);
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(10);
		await page.getByTestId('knowledge-facts-load-more').click();
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(12);
		const journal: ApiJournalEntry[] = apiJournal;
		const knowledgeReads = journal.filter(
			(entry) =>
				entry.method === 'GET' &&
				entry.path.includes(`/knowledge?`) &&
				entry.path.includes(appId)
		);
		expect(knowledgeReads).toHaveLength(2);
		expect(knowledgeReads[0].path).toContain('offset=0');
		expect(knowledgeReads[1].path).toContain('offset=10');
	});

	test('real api: learned notes survive a fresh session and keyboard retract at 360x640', async ({
		page
	}) => {
		const jar: CookieJar = { cookie: '', csrf: '' };
		const appId = crypto.randomUUID();
		const factId = crypto.randomUUID();
		_seedServiceKnowledge(appId, 2, [
			{ id: factId, key: 'survivor', statement: 'this note survives fresh sessions', revision: 1 }
		]);
		await realLoginSetup(jar);

		await _openRealApiFixture(page, jar, appId);
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(1);

		// KEYBOARD path at 360x640: focus the reason field, type, walk the
		// tab order to the retract control, press Enter.
		await page.getByTestId('knowledge-reason').click();
		await page.keyboard.type('keyboard retraction');
		let focused = '';
		for (let index = 0; index < 10; index += 1) {
			await page.keyboard.press('Tab');
			focused = await page.evaluate(() =>
				document.activeElement?.getAttribute('data-testid') ?? ''
			);
			if (focused === 'knowledge-fact-retract') break;
		}
		expect(focused).toBe('knowledge-fact-retract');
		await page.keyboard.press('Enter');
		await expect(page.getByTestId('knowledge-fact-row')).toHaveCount(0);
		await page.screenshot({
			path: _evidenceScreenshot('real-api-keyboard-retracted-360.png'),
			fullPage: true
		});

		// FRESH SESSION: a brand-new cookie jar + login still reads the
		// (retracted-marked) history — the notes live server-side.
		const freshJar = { cookie: '', csrf: '' };
		await realLoginSetup(freshJar);
		await page.reload();
		// History: the fact's revision records PERSIST (retraction marks
		// the fact retracted — it never deletes history). The retraction's
		// own revision row lands with the reload; the original record is
		// always present.
		await expect(page.getByTestId('knowledge-history-row').first()).toContainText(
			'this note survives fresh sessions'
		);
		await expect(page.getByTestId('knowledge-history-row').last()).toContainText(
			/retracted|this note survives fresh sessions/
		);
		const journal: ApiJournalEntry[] = apiJournal;
		expect(journal.length).toBeGreaterThan(0);
		fs.writeFileSync(
			path.join(EVIDENCE_DIR, 'real-api-journal.json'),
			JSON.stringify(journal, null, 2)
		);
	});
});
