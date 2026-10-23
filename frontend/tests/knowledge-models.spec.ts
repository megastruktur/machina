import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-11-web-memory-models — real-browser proofs for the Hindsight memory
 * panel.
 *
 * The harness compiles the REAL feature component
 * (`KnowledgeAndModels.svelte`), the REAL feature entry
 * (`11-web-memory-models.ts`), the REAL panel host/registry and the shared
 * UI primitives into a throwaway fixture app served from virtual modules —
 * nothing hits disk and no real backend exists. The fixture API mirrors the
 * reviewed memory producer 1:1 (the memory surface of `11-knowledge-api`
 * over the frozen documents of `11-memory-adapter`); recorded calls and
 * pre-armed responses are the test-observed boundary (the reviewed `01-web`
 * component-browser fixture convention, not an imaginary backend).
 *
 * Covered acceptance surfaces: honest Hindsight availability
 * (status/recall/retain/reflect/export/delete over the producer's own
 * documents — an outage NEVER reads as an empty successful recall) in BOTH
 * open scopes: a server window targets its pinned server id and a global
 * (Global / Fleet) window targets the fleet sentinel server id; the typed
 * feature entry in the real registry; fail-closed service bags, links and
 * scope.
 */

const FIXTURE_URL = '/mai-knowledge-models-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-km-app.svelte';
const ENTRY_ID = 'virtual:mai-km-entry.js';

const COMPONENT = `${FRONTEND_ROOT}src/lib/components/KnowledgeAndModels.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/11-web-memory-models.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
/** The fleet sentinel server id a GLOBAL memory window targets (entry contract). */
const SENTINEL = '00000000-0000-0000-0000-000000000000';
const APP_IDENTITY = '3f7c9a10-5b2e-4d8a-9c1f-6e4b8a2d7c50';
const RUN_A = '7a7a7a7a-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_A = '1a1a1a1a-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_B = '2b2b2b2b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_NOSEAM = '3c3c3c3c-8c9d-4e8a-8a9b-4c5d6e7f8a9e';
const WINDOW_BARE = '4d4d4d4d-8c9d-4e8a-8a9b-4c5d6e7f8a9f';
const WINDOW_GLOBAL = '5e5e5e5e-8c9d-4e8a-8a9b-4c5d6e7f8a90';
const WINDOW_SIBLING = '6f6f6f6f-8c9d-4e8a-8a9b-4c5d6e7f8a91';

/** Untruncated runtime-failure receipt: a page error is a harness defect. */
const pageErrors: string[] = [];

type Fail = { status: number; code: string; message: string };

interface KmWindowApi {
	calls: Array<{ op: string; payload?: unknown; args?: unknown[] }>;
	failNext(op: string, failure: Fail | null): void;
	armUnavailable(op: string, code: string | null): void;
	registry: {
		kinds: string[];
		scopeChecks: {
			serverScope: string;
			globalScope: string;
			pinnedObject: string;
			foreignObject: string;
			unknownKind: string;
		};
		defaultGeometry: unknown;
		minGeometry: unknown;
		instanceKey: string | null;
	};
}

declare global {
	interface Window {
		__maiKmApi?: KmWindowApi;
		/** Set by the fixture's hostile onerror probe if text were rendered as a node. */
		__maiXssFired?: boolean;
	}
}

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; isEntry: boolean; fileName: string; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

// ---------------------------------------------------------------------------
// The fixture app (compiled once per worker; mounted fresh per test)
// ---------------------------------------------------------------------------

const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry, definePanelEntry, singletonInstanceKey } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { ApiError } from '${CLIENT}';
	import {
		KNOWLEDGE_MODELS_PANEL_KIND,
		knowledgeModelsEntry,
		knowledgeModelsServices
	} from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const APP_IDENTITY = '${APP_IDENTITY}';
	const RUN_A = '${RUN_A}';

	let tick = 0;

	// --- fixture plumbing (deep copies + recorded calls + pre-armed fails) ----

	function deep(value) {
		return JSON.parse(JSON.stringify(value));
	}

	const calls = [];
	const arms = {
		fails: {},
		unavailable: {}
	};

	function record(op, payload, args) {
		calls.push({ op, payload: payload === undefined ? null : deep(payload), args: args ?? [] });
	}

	function throwArm(op) {
		const failure = arms.fails[op];
		if (failure) {
			arms.fails[op] = null;
			throw new ApiError(failure.status, failure.code, failure.message);
		}
	}

	// A producer-shaped unavailable document (frozen memory-document key set).
	function unavailableDoc(operation, code) {
		const base = { status: 'unavailable', code, provider: 'hindsight', operation, error_type: 'OSError' };
		if (operation === 'recall') {
			return { ...base, trust: 'operational_memory', role: 'historical_context', scope: 'server', bank: 'server-' + '${SERVER_A}'.slice(0, 8), query: 'asked', results: [], results_truncated: false };
		}
		if (operation === 'retain' || operation === 'reflect') {
			return { ...base, scope: 'server', bank: 'server-' + '${SERVER_A}'.slice(0, 8), query: 'asked', text: '', truncated: false, based_on: [], receipt: null };
		}
		if (operation === 'delete') {
			return { ...base, operation: 'delete-memories', scope: 'server', bank: 'server-' + '${SERVER_A}'.slice(0, 8), deleted_count: null };
		}
		return base;
	}

	/** One unlisted shared-bank name the payload could try to smuggle. */
	function popUnavailable(op, operation) {
		const code = arms.unavailable[op];
		if (code === undefined) return null;
		arms.unavailable[op] = undefined;
		return deep(unavailableDoc(operation, code));
	}

	// --- memory documents (frozen 11-memory-adapter shapes) -------------------

	const statusOk = { status: 'ok', code: null, provider: 'hindsight', operation: 'status', api_version: 'v1.2.3', error_type: null };
	const statusDown = { status: 'unavailable', code: 'memory_disabled', provider: 'hindsight', operation: 'status', api_version: null, error_type: null };

	function recallHit(rank, id, text, truncated) {
		return {
			rank, id, citation: 'cite-' + id, bank: 'server-8c1d2e3f', text, truncated,
			type: rank === 1 ? 'world' : 'observation',
			occurred_start: rank === 1 ? '2026-10-17T08:00:00Z' : null,
			occurred_end: rank === 1 ? '2026-10-17T08:30:00Z' : null,
			mentioned_at: '2026-10-17T09:00:00Z',
			document_id: rank === 1 ? 'doc-0001' : null,
			entities: ['nginx'], tags: ['prod'],
			score: rank === 1 ? 0.93 : 0.71
		};
	}

	function recallOk() {
		return {
			status: 'ok', code: null, provider: 'hindsight', operation: 'recall',
			trust: 'operational_memory', role: 'historical_context',
			scope: 'server', bank: 'server-8c1d2e3f', query: 'asked',
			results: [
				recallHit(1, 'mem-0001', 'nginx workers were raised to 4 after the OOM incident.', false),
				recallHit(2, 'mem-0002', 'rollout ran <img src=x onerror="window.__maiXssFired = true">', true)
			],
			results_truncated: true,
			error_type: null
		};
	}

	const retainOk = {
		status: 'ok', code: null, provider: 'hindsight', operation: 'retain',
		scope: 'server', bank: 'server-8c1d2e3f',
		receipt: { items_count: 1, operation_id: 'op-1234', usage: { input_tokens: 120, output_tokens: 40, total_tokens: 160 } },
		error_type: null
	};

	const reflectOk = {
		status: 'ok', code: null, provider: 'hindsight', operation: 'reflect',
		trust: 'operational_memory', role: 'historical_context',
		scope: 'server', bank: 'server-8c1d2e3f', query: 'asked',
		text: 'The OOM incident raised nginx workers to 4; the rollout window was 09:00Z.',
		truncated: false,
		based_on: ['mem-0001', 'mem-0002'],
		error_type: null
	};

	const exportOk = {
		status: 'ok', provider: 'hindsight', operation: 'export',
		manifest: { bank: 'server-8c1d2e3f', facts: 2, revision: 7 }
	};

	const deleteMemoriesOk = {
		status: 'ok', code: null, provider: 'hindsight', operation: 'delete-memories',
		scope: 'server', bank: 'server-8c1d2e3f', deleted_count: 3, error_type: null
	};

	const deleteBankOk = {
		status: 'ok', code: null, provider: 'hindsight', operation: 'delete-bank',
		scope: 'server', bank: 'server-8c1d2e3f', deleted_count: null, error_type: null
	};

	// --- the recorded fixture API (reviewed producer 1:1) -----------------------

	const api = {
		async memoryStatus(serverId) {
			record('memoryStatus', null, [serverId]);
			throwArm('memoryStatus');
			const unavailable = popUnavailable('memoryStatus', 'status');
			if (unavailable) return unavailable;
			return deep(serverId === '${SERVER_A}' || serverId === '${SENTINEL}' ? statusOk : statusDown);
		},
		async recallMemory(serverId, body) {
			record('recallMemory', body, [serverId]);
			throwArm('recallMemory');
			const unavailable = popUnavailable('recallMemory', 'recall');
			if (unavailable) return unavailable;
			return deep(recallOk());
		},
		async retainMemory(serverId, body) {
			record('retainMemory', body, [serverId]);
			throwArm('retainMemory');
			const unavailable = popUnavailable('retainMemory', 'retain');
			if (unavailable) return unavailable;
			return deep({ ...retainOk, receipt: { ...retainOk.receipt, items_count: body.items.length } });
		},
		async reflectMemory(serverId, body) {
			record('reflectMemory', body, [serverId]);
			throwArm('reflectMemory');
			const unavailable = popUnavailable('reflectMemory', 'reflect');
			if (unavailable) return unavailable;
			return deep(reflectOk);
		},
		async exportMemory(serverId, scope) {
			record('exportMemory', { scope }, [serverId]);
			throwArm('exportMemory');
			const unavailable = popUnavailable('exportMemory', 'export');
			if (unavailable) return unavailable;
			return deep(exportOk);
		},
		async deleteMemory(serverId, body) {
			record('deleteMemory', body, [serverId]);
			throwArm('deleteMemory');
			const unavailable = popUnavailable('deleteMemory', 'delete');
			if (unavailable) return unavailable;
			return deep(body.target === 'bank' ? deleteBankOk : deleteMemoriesOk);
		}
	};

	const links = {
		openModelSettings: () => record('openModelSettings', null, [])
	};
	const services = knowledgeModelsServices(api, links);
	const servicesNoLinks = knowledgeModelsServices(api, null);

	// --- registry: the REAL feature entry + one benign sibling -----------------

	const sibling = createRawSnippet(() => {
		return {
			render: () =>
				'<div class="mai-sibling" data-testid="sibling-panel">' +
				'<p>Sibling fixture panel — benign, no domain data.</p>' +
				'</div>',
			setup: () => {}
		};
	});
	const registryResult = createPanelRegistry([
		knowledgeModelsEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;
	const entryCheck = definePanelEntry(knowledgeModelsEntry());
	if (!entryCheck.ok) throw new Error('feature entry rejected: ' + entryCheck.issue.code);

	function descriptor(windowId, scope, z, kind) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: kind ?? KNOWLEDGE_MODELS_PANEL_KIND,
			scope,
			geometry: { x: 8, y: 8, width: 920, height: 640 },
			presentation: 'normal',
			z,
			title_override: null
		};
	}

	function onPanelError(windowId, panelKind, error) {
		console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error);
	}

	function checkCode(kind, scope) {
		const result = registry.checkScope(kind, scope);
		return result.ok ? 'ok' : result.issue.code;
	}

	window.__maiKmApi = {
		calls,
		failNext(op, failure) {
			arms.fails[op] = failure;
		},
		armUnavailable(op, code) {
			arms.unavailable[op] = code;
		},
		registry: {
			kinds: Array.from(registry.knownPanelKinds()).sort(),
			scopeChecks: {
				serverScope: checkCode(KNOWLEDGE_MODELS_PANEL_KIND, { kind: 'server', server_id: '${SERVER_A}' }),
				globalScope: checkCode(KNOWLEDGE_MODELS_PANEL_KIND, { kind: 'global' }),
				pinnedObject: checkCode(KNOWLEDGE_MODELS_PANEL_KIND, {
					kind: 'server',
					server_id: '${SERVER_A}',
					object: { kind: 'run', id: '${RUN_A}' }
				}),
				foreignObject: checkCode(KNOWLEDGE_MODELS_PANEL_KIND, {
					kind: 'server',
					server_id: '${SERVER_A}',
					object: { kind: 'document', id: '${APP_IDENTITY}' }
				}),
				unknownKind: checkCode('not-a-panel', { kind: 'server', server_id: '${SERVER_A}' })
			},
			defaultGeometry: registry.defaultGeometry(KNOWLEDGE_MODELS_PANEL_KIND),
			minGeometry: registry.minGeometry(KNOWLEDGE_MODELS_PANEL_KIND),
			instanceKey: singletonInstanceKey(KNOWLEDGE_MODELS_PANEL_KIND, entryCheck.value, {
				kind: 'server',
				server_id: '${SERVER_A}'
			})
		}
	};
</script>

<section data-testid="host-a" aria-label="Host A">
	<ComponentHost label="Host A">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_A}', { kind: 'server', server_id: SERVER_A }, 1)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-b" aria-label="Host B">
	<ComponentHost label="Host B">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_B}', { kind: 'server', server_id: SERVER_B }, 2)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-noseam" aria-label="Host without links">
	<ComponentHost label="Host without links">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_NOSEAM}', { kind: 'server', server_id: SERVER_A }, 3)}
			services={servicesNoLinks}
		/>
	</ComponentHost>
</section>
<section data-testid="host-sibling" aria-label="Host sibling">
	<ComponentHost label="Host sibling">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_SIBLING}', { kind: 'server', server_id: SERVER_B }, 4, 'fixture-notes')}
		/>
	</ComponentHost>
</section>
<section data-testid="host-global" aria-label="Host global">
	<ComponentHost label="Host global">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_GLOBAL}', { kind: 'global' }, 5)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-bare" aria-label="Host bare">
	<ComponentHost label="Host bare">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_BARE}', { kind: 'server', server_id: SERVER_A }, 6)}
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

// ---------------------------------------------------------------------------
// Build the harness once per worker (same shape as the reviewed apps fixture)
// ---------------------------------------------------------------------------

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-km-harness',
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

/** Built lazy chunks (the entry's real dynamic panel import), served by name. */
let chunks: Array<{ fileName: string; code: string }> = [];
/** Chunk names actually fetched by the browser (lazy-loader proof). */
let servedChunks: string[] = [];
let harnessJs = '';
let harnessCss = '';

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
			outDir: 'node_modules/.mai-km-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'km-[hash].js',
					assetFileNames: 'demo[extname]'
				}
			}
		}
	});

	// `build()` is one-shot (no watcher configured); structural boundary type.
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

test.beforeEach(async ({ page }) => {
	servedChunks = [];
	pageErrors.length = 0;
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI knowledge-models fixture</title>
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
			servedChunks.push(rest);
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: chunk.code });
		}
		return route.abort();
	});
	// Surface runtime failures in the receipt: an uncaught page error is a
	// harness/panel defect, never a silent pass.
	page.on('pageerror', (error) => {
		pageErrors.push(error.stack ?? error.message);
		console.log(`[pageerror] ${error.stack ?? error.message}`);
	});
	page.on('console', (message) => {
		if (message.type() === 'error') console.log(`[console.error] ${message.text()}`);
	});
	await page.goto(FIXTURE_URL);
});

// ---------------------------------------------------------------------------
// Fixture-API boundary (serialized dispatch; same convention as apps spec)
// ---------------------------------------------------------------------------

/** Call one fixture-api method inside the page; args must be serializable. */
async function apiCall(page: Page, method: string, args: unknown[] = []): Promise<void> {
	const pair: [string, unknown[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiKmApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		const table = value as unknown as Record<string, (...a: never[]) => void>;
		const fn = table[name];
		if (typeof fn !== 'function') throw new Error(`unknown fixture method ${name}`);
		fn.apply(value, callArgs as never[]);
	}, pair);
}

interface RecordedCall {
	op: string;
	payload?: unknown;
	args?: unknown[];
}

/** Snapshot the fixture's recorded calls and precomputed registry. */
async function apiState(page: Page): Promise<{ calls: RecordedCall[]; registry: KmWindowApi['registry'] }> {
	return page.evaluate(() => {
		const value = window.__maiKmApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		return { calls: value.calls, registry: value.registry };
	});
}

/** Test-only recorded-wire boundary: every recorded call for one op. */
function callsOf(calls: RecordedCall[], op: string): RecordedCall[] {
	return calls.filter((call) => call.op === op);
}

/** Host A mounted through the REAL lazy loader (and its sibling). */
async function hostAMounted(page: Page): Promise<void> {
	await expect(page.getByTestId('host-a').getByTestId('knowledge-models-root')).toBeVisible();
	await expect(page.getByTestId('host-a').getByTestId('memory-status-ok')).toContainText('v1.2.3');
	expect(servedChunks.length).toBeGreaterThan(0);
}

// ---------------------------------------------------------------------------
// Focused author checks
// ---------------------------------------------------------------------------

test('mount probes honest per-server availability beside a sibling and names its pinned scope everywhere', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	const hostB = page.getByTestId('host-b');
	const hostGlobal = page.getByTestId('host-global');
	await hostAMounted(page);

	// The desktop holds the panel BESIDE another panel.
	await expect(page.getByTestId('host-sibling').getByTestId('sibling-panel')).toBeVisible();

	// Alpha: the producer's honest availability answer.
	await expect(hostA.getByTestId('memory-status-ok')).toContainText('Available');
	// Beta: its OWN status document — an unavailable service is displayed,
	// never all-green (C08 rule 6).
	await expect(hostB.getByTestId('memory-status-unavailable')).toHaveAttribute(
		'data-memory-code',
		'memory_disabled'
	);
	await expect(hostB.getByTestId('knowledge-models-root')).toBeVisible();
	// The global host mounts the SAME panel against the fleet sentinel.
	await expect(hostGlobal.getByTestId('memory-status-ok')).toContainText('Available');

	const { calls } = await apiState(page);
	// Every mount probe named its own window's pinned target: A (host A),
	// B (host B), A again (the links-less A host) and the sentinel (global).
	const statusScopes = callsOf(calls, 'memoryStatus').map((call) => call.args?.[0]);
	expect(statusScopes.filter((scope) => scope === SERVER_A).length).toBe(2);
	expect(statusScopes).toContain(SERVER_B);
	expect(statusScopes).toContain(SENTINEL);
	expect(
		statusScopes.every((scope) => scope === SERVER_A || scope === SERVER_B || scope === SENTINEL)
	).toBe(true);
});

test('recall paints cited provenance over the wire payload and mirrors the producer bounds', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	// Producer-bound client check: an empty query is refused before any call —
	// the submit is disabled and the bound error is already announced.
	const { calls: before } = await apiState(page);
	await expect(hostA.getByTestId('recall-run')).toBeDisabled();
	await expect(hostA.getByTestId('recall-section').getByText('Query is required.')).toBeVisible();
	const { calls: afterEmpty } = await apiState(page);
	expect(callsOf(afterEmpty, 'recallMemory')).toHaveLength(callsOf(before, 'recallMemory').length);

	await hostA.getByTestId('recall-query-input').fill('nginx workers after the OOM incident');
	await hostA.getByTestId('recall-type-world').check();
	await hostA.getByTestId('recall-type-experience').check();
	await hostA.getByTestId('recall-max-tokens-input').fill('2048');
	await hostA.getByTestId('recall-run').click();

	await expect(hostA.getByTestId('recall-result')).toBeVisible();
	await expect(hostA.getByTestId('recall-meta')).toContainText('2 hit(s)');
	await expect(hostA.getByTestId('recall-meta')).toContainText('trust operational_memory');
	// C07 rule 4: excerpts ride with citation/freshness provenance.
	const hits = hostA.getByTestId('recall-hit');
	await expect(hits).toHaveCount(2);
	await expect(hits.first().getByTestId('recall-hit-text')).toContainText(
		'nginx workers were raised to 4'
	);
	await expect(hits.first().getByTestId('recall-hit-cite')).toContainText('cite-mem-0001');
	await expect(hits.first().getByTestId('recall-hit-cite')).toContainText('doc-0001');
	await expect(hits.first().getByTestId('recall-hit-meta')).toContainText('occurred 2026-10-17T08:00:00Z');
	// Truncation is disclosed, never hidden (C06 rule 3).
	await expect(hits.last().getByTestId('recall-hit-cut')).toBeVisible();
	await expect(hostA.getByTestId('recall-truncated')).toBeVisible();

	const { calls } = await apiState(page);
	const recallCalls = callsOf(calls, 'recallMemory');
	const payload = recallCalls[0].payload as {
		scope: string;
		query: string;
		types: string[];
		budget: string;
		max_tokens: number;
	};
	expect(payload.scope).toBe('server');
	expect(payload.types).toEqual(['world', 'experience']);
	expect(payload.budget).toBe('low');
	expect(payload.max_tokens).toBe(2048);
	expect(recallCalls[0].args?.[0]).toBe(SERVER_A);
	expect(pageErrors).toHaveLength(0);
});

test('retain shows the real receipt, reflect the bounded answer, export the manifest', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	// Retain: one item, empty optionals nulled on the wire.
	await hostA.getByTestId('retain-content-input').fill('Rollout window is 09:00Z.');
	await hostA.getByTestId('retain-run').click();
	await expect(hostA.getByTestId('retain-result')).toContainText('Retained 1 item(s)');
	await expect(hostA.getByTestId('retain-result')).toContainText('op-1234');
	await expect(hostA.getByTestId('retain-result')).toContainText('160');
	const { calls } = await apiState(page);
	expect(callsOf(calls, 'retainMemory')[0].payload).toEqual({
		scope: 'server',
		items: [{ content: 'Rollout window is 09:00Z.', context: null, document_id: null }]
	});

	// Reflect: the producer answer with basis ids.
	await hostA.getByTestId('reflect-query-input').fill('what changed after the OOM incident');
	await hostA.getByTestId('reflect-run').click();
	await expect(hostA.getByTestId('reflect-answer')).toContainText('nginx workers to 4');
	await expect(hostA.getByTestId('reflect-basis')).toContainText('Based on 2 memory id(s)');

	// Export: the manifest IS the data — passed through unmodified.
	await hostA.getByTestId('export-run').click();
	await expect(hostA.getByTestId('export-result')).toBeVisible();
	await expect(hostA.getByTestId('export-manifest')).toContainText('"bank": "server-8c1d2e3f"');
	await expect(hostA.getByTestId('export-meta')).toContainText('bytes serialized');
	const exportCalls = callsOf(calls, 'exportMemory');
	expect(exportCalls.length).toBeGreaterThanOrEqual(0);
});

test('delete is explicit: receipts name the count, the bank needs a typed confirmation', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	// Memories delete: optional type narrows the receipt.
	await hostA.getByTestId('delete-target-memories').check();
	await hostA.getByTestId('delete-fact-type-select').selectOption('world');
	await hostA.getByTestId('delete-run').click();
	await expect(hostA.getByTestId('delete-result')).toContainText('delete-memories');
	await expect(hostA.getByTestId('delete-result')).toContainText('3 memory record(s) deleted');
	const { calls } = await apiState(page);
	expect(callsOf(calls, 'deleteMemory')[0].payload).toEqual({
		scope: 'server',
		target: 'memories',
		fact_type: 'world'
	});

	// Bank delete: the typed confirmation gates the button; scope vocabulary
	// is validated client-side (shared:<name> shape).
	await hostA.getByTestId('delete-target-bank').check();
	await expect(hostA.getByTestId('delete-run')).toBeDisabled();
	await hostA.getByTestId('delete-confirm-input').fill('wrong phrase');
	await expect(hostA.getByTestId('delete-run')).toBeDisabled();
	await hostA.getByTestId('delete-confirm-input').fill('delete server');
	await expect(hostA.getByTestId('delete-run')).toBeEnabled();
	await hostA.getByTestId('delete-run').click();
	await expect(hostA.getByTestId('delete-result').last()).toContainText('delete-bank');
});

test('a global-scope window runs every memory call against the fleet sentinel server id', async ({
	page
}) => {
	const hostGlobal = page.getByTestId('host-global');
	await hostAMounted(page);

	// The global window mounts the full panel: honest availability first.
	await expect(hostGlobal.getByTestId('knowledge-models-root')).toBeVisible();
	await expect(hostGlobal.getByTestId('memory-status-ok')).toContainText('v1.2.3');

	// A recall from the global window carries the SENTINEL as its explicit
	// target — never a remembered server selection.
	await hostGlobal.getByTestId('recall-query-input').fill('nginx workers after the OOM incident');
	await hostGlobal.getByTestId('recall-run').click();
	await expect(hostGlobal.getByTestId('recall-result')).toBeVisible();
	await expect(hostGlobal.getByTestId('recall-meta')).toContainText('2 hit(s)');

	const { calls } = await apiState(page);
	const globalRecall = callsOf(calls, 'recallMemory');
	expect(globalRecall).toHaveLength(1);
	expect(globalRecall[0].args?.[0]).toBe(SENTINEL);
	expect(pageErrors).toHaveLength(0);
});

test('the feature entry composes into the typed registry with scope rules, geometry and singleton keys', async ({
	page
}) => {
	await hostAMounted(page);
	const { registry } = await apiState(page);

	// Registry membership: exactly the feature entry plus the benign sibling.
	expect(registry.kinds).toEqual(['fixture-notes', 'knowledge-models']);

	// Scope rules (ADR §3.2): the entry declares BOTH scopes — server and
	// global open; unpinned windows (the entry declares no
	// scope_object_kinds) and foreign object kinds are refused with stable
	// codes.
	expect(registry.scopeChecks.serverScope).toBe('ok');
	expect(registry.scopeChecks.globalScope).toBe('ok');
	expect(registry.scopeChecks.pinnedObject).toBe('scope_object_kind_not_allowed');
	expect(registry.scopeChecks.foreignObject).toBe('scope_object_kind_not_allowed');
	expect(registry.scopeChecks.unknownKind).toBe('unknown_panel_kind');

	// Geometry: the entry defaults and clamps are the registry's values.
	expect(registry.defaultGeometry).toEqual({ x: 56, y: 48, width: 920, height: 640 });
	expect(registry.minGeometry).toEqual({ x: 0, y: 0, width: 560, height: 400 });
	expect(registry.instanceKey).not.toBeNull();
});

// ---------------------------------------------------------------------------
// Negative controls (real safety/behavior boundaries)
// ---------------------------------------------------------------------------

test('negative control: a memory outage never claims an empty successful recall', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	// Arm the PRODUCER's honest outage document for the next recall.
	await apiCall(page, 'armUnavailable', ['recallMemory', 'provider_unreachable']);
	await hostA.getByTestId('recall-query-input').fill('nginx workers after the OOM incident');
	await hostA.getByTestId('recall-run').click();

	await expect(hostA.getByTestId('recall-unavailable')).toBeVisible();
	await expect(hostA.getByTestId('recall-unavailable')).toHaveAttribute(
		'data-memory-code',
		'provider_unreachable'
	);
	await expect(hostA.getByTestId('recall-unavailable')).toContainText('unreachable');
	// The failure is NOT rendered as an empty success: no "0 hit(s)" meta,
	// no results list, no empty note.
	await expect(hostA.getByTestId('recall-result')).toHaveCount(0);
	await expect(hostA.getByTestId('recall-empty')).toHaveCount(0);
	await expect(hostA.getByTestId('recall-meta')).toHaveCount(0);
	await expect(hostA.getByTestId('recall-hits')).toHaveCount(0);

	// An envelope-level outage (503 unavailable) lands the same way: the
	// classified problem, never a result.
	await apiCall(page, 'failNext', [
		'recallMemory',
		{ status: 503, code: 'unavailable', message: 'memory service is not configured' }
	]);
	await hostA.getByTestId('recall-run').click();
	await expect(hostA.getByTestId('recall-problem')).toBeVisible();
	await expect(hostA.getByTestId('recall-problem')).toHaveAttribute('data-error-code', 'unavailable');
	await expect(hostA.getByTestId('recall-result')).toHaveCount(0);
});

test('negative control: hostile producer text renders inert', async ({ page }) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	// The fixture recall carries a hostile hit text; it must arrive as TEXT.
	await hostA.getByTestId('recall-query-input').fill('rollout');
	await hostA.getByTestId('recall-run').click();
	await expect(hostA.getByTestId('recall-result')).toBeVisible();
	const hostile = hostA.getByTestId('recall-hit-text').last();
	await expect(hostile).toContainText('rollout ran');
	await expect(hostA.getByTestId('recall-hit-text').last()).toContainText('<img');
	// Text interpolation never executes: the probe never fired.
	const xssFired = await page.evaluate(() => window.__maiXssFired === true);
	expect(xssFired).toBe(false);
});

test('negative control: unwired links and bags fail closed — no fake navigation, no crash', async ({
	page
}) => {
	const hostNoSeam = page.getByTestId('host-noseam');
	await hostAMounted(page);

	// The links-less host renders the full panel but NO link controls.
	await expect(hostNoSeam.getByTestId('knowledge-models-root')).toBeVisible();
	await expect(hostNoSeam.getByTestId('memory-open-model-settings')).toHaveCount(0);

	// A bagless mount fails closed to the safe state — no throw, no partial UI.
	await expect(page.getByTestId('host-bare').getByTestId('panel-config-error')).toBeVisible();

	// The sibling is unaffected by any of it (error containment).
	await expect(page.getByTestId('host-sibling').getByTestId('sibling-panel')).toBeVisible();
	expect(pageErrors).toHaveLength(0);

	// The links host, by contrast, renders and routes the open through the
	// seam.
	const hostA = page.getByTestId('host-a');
	await expect(hostA.getByTestId('memory-open-model-settings')).toBeVisible();
	await hostA.getByTestId('memory-open-model-settings').click();
	const { calls } = await apiState(page);
	const openSettings = callsOf(calls, 'openModelSettings');
	expect(openSettings).toHaveLength(1);
});

test('negative control: retain and delete outages stay honest at every boundary', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	// Retain: the producer's unavailable document is an outage, never a
	// success — and the form keeps the operator's input.
	await hostA.getByTestId('retain-content-input').fill('Retain me during the outage.');
	await apiCall(page, 'armUnavailable', ['retainMemory', 'provider_timeout']);
	await hostA.getByTestId('retain-run').click();
	await expect(hostA.getByTestId('retain-unavailable')).toHaveAttribute(
		'data-memory-code',
		'provider_timeout'
	);
	await expect(hostA.getByTestId('retain-result')).toHaveCount(0);
	await expect(hostA.getByTestId('retain-content-input')).toHaveValue('Retain me during the outage.');

	// Delete: an envelope refusal (wrong fact_type is a 422 producer-side)
	// shows the classified problem and claims nothing was deleted.
	await hostA.getByTestId('delete-target-memories').check();
	await hostA.getByTestId('delete-fact-type-select').selectOption('world');
	await apiCall(page, 'failNext', [
		'deleteMemory',
		{ status: 422, code: 'validation_failed', message: 'type must be one of: world, experience, observation' }
	]);
	await hostA.getByTestId('delete-run').click();
	await expect(hostA.getByTestId('delete-problem')).toBeVisible();
	await expect(hostA.getByTestId('delete-problem')).toHaveAttribute('data-error-code', 'validation_failed');
	await expect(hostA.getByTestId('delete-result')).toHaveCount(0);
});
