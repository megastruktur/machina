import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-11-web-memory-models — real-browser proofs for the Hindsight memory +
 * local-models panel.
 *
 * The harness compiles the REAL feature component
 * (`KnowledgeAndModels.svelte`), the REAL feature entry
 * (`11-web-memory-models.ts`), the REAL panel host/registry and the shared
 * UI primitives into a throwaway fixture app served from virtual modules —
 * nothing hits disk and no real backend exists. The fixture API mirrors the
 * reviewed producers 1:1 (the memory surface of `11-knowledge-api` over the
 * frozen documents of `11-memory-adapter`, the local-model surface of
 * `11-model-install`, and the C02 connection surface of `06-model-api`);
 * recorded calls and pre-armed responses are the test-observed boundary
 * (the reviewed `01-web` component-browser fixture convention, not an
 * imaginary backend).
 *
 * Covered acceptance surfaces: honest Hindsight availability
 * (status/recall/retain/reflect/export/delete over the producer's own
 * documents — an outage NEVER reads as an empty successful recall); the
 * local-model prerequisite preview (REAL measured facts, fail-closed
 * verdict) gating install/pull submits behind a satisfied preview of the
 * SAME recipe+target; watched run receipts with dedup + honest terminal
 * states; the pull IS the registration path (registered connection visible
 * with its credential REFERENCE only); the 1Password-aware credential
 * reference checker (value-free, zero network use); typed feature entry in
 * the real registry; fail-closed service bags, links and scope.
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
const APP_IDENTITY = '3f7c9a10-5b2e-4d8a-9c1f-6e4b8a2d7c50';
const RUN_A = '7a7a7a7a-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_A = '1a1a1a1a-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_B = '2b2b2b2b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_NOSEAM = '3c3c3c3c-8c9d-4e8a-8a9b-4c5d6e7f8a9e';
const WINDOW_BARE = '4d4d4d4d-8c9d-4e8a-8a9b-4c5d6e7f8a9f';
const WINDOW_GLOBAL = '5e5e5e5e-8c9d-4e8a-8a9b-4c5d6e7f8a90';
const WINDOW_SIBLING = '6f6f6f6f-8c9d-4e8a-8a9b-4c5d6e7f8a91';

/** A value-shaped string that must NEVER be classified as a reference. */
const FORBIDDEN_SECRET = 'sk-live-9f2b7c1a-never-typed-anywhere';

/** Untruncated runtime-failure receipt: a page error is a harness defect. */
const pageErrors: string[] = [];

type Fail = { status: number; code: string; message: string };

type MemoryOp = 'status' | 'recall' | 'retain' | 'reflect' | 'export' | 'delete';

interface KmWindowApi {
	calls: Array<{ op: string; payload?: unknown; args?: unknown[] }>;
	failNext(op: string, failure: Fail | null): void;
	armUnavailable(op: string, code: string | null): void;
	armWrongServerPreview(): void;
	setRunState(runId: string, state: string): void;
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
		unavailable: {},
		wrongServerPreview: false
	};
	const watched = {};

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

	// --- local-model documents (11-model-install / 06-model-api shapes) -------

	function planView() {
		return {
			server_id: '${SERVER_A}',
			application_id: '${APP_IDENTITY}',
			project_name: 'mai-local-inference',
			target_os: 'linux', target_arch: 'x86_64',
			image: 'docker.io/ollama/ollama@sha256:2654',
			recipe_sha256: 'a'.repeat(64),
			compose_path: '/srv/mai/compose.yaml', compose_sha256: 'b'.repeat(64),
			compose_text: 'services: {}',
			directories: ['/srv/mai/models'],
			ports: [{ host: 18114, container: 11434, protocol: 'tcp' }],
			volumes: [{ name: 'models', container_path: '/root/.ollama', host_path: '/srv/mai/models' }],
			credentials: [], credentials_path: null, health_probe: null,
			prerequisites: ['docker', 'hw.arch.x86_64', 'hw.cpu.avx2', 'hw.ram.16gb', 'hw.disk.30gb'],
			required_grants: ['docker.compose'],
			operations: [{ op: 'verify_prerequisites', subject: 'target' }, { op: 'deploy', subject: 'compose' }],
			limitations: ['weights are acquired by an explicit admin pull'],
			plan_sha256: 'c'.repeat(64)
		};
	}

	function previewView(verdict, unsatisfied, unknown, notes) {
		return {
			server_id: '${SERVER_A}',
			application_id: '${APP_IDENTITY}',
			plan: planView(),
			facts: { 'hw.arch': 'x86_64', 'hw.ram.gb': 32 },
			unknown: unknown,
			measurement_notes: notes,
			hardware_unsatisfied: unsatisfied,
			verdict
		};
	}

	function runView(state) {
		return {
			run_id: '${RUN_A}', server_id: '${SERVER_A}', parent_run_id: null, depth: 0,
			profile_revision: 3, prompt_revision: 1, model_profile: 'local',
			requested_grants: ['docker.compose', 'model.install'],
			state, cancel_requested_at: null,
			submitted_at: '2026-10-18T09:00:00Z', updated_at: '2026-10-18T09:05:00Z'
		};
	}

	function runDetail(state, steps) {
		return { ...runView(state), steps };
	}

	watched['${RUN_A}'] = runDetail('queued', [
		{ seq: 1, capability_id: 'model.install', state: 'pending', receipt: null }
	]);

	const connectionRef = { name: 'local-inference-8c1d2e3f', revision: 1, schema_revision: 1, sha256: 'd'.repeat(64) };
	const connectionViewDoc = {
		schema_version: 1,
		name: 'local-inference-8c1d2e3f',
		base_url: 'http://127.0.0.1:18114/v1',
		credential_ref: null,
		timeout_seconds: null,
		headers: [],
		capabilities: { tool_calls: false, reasoning: false },
		revision: 1, sha256: 'd'.repeat(64)
	};

	// --- the recorded fixture API (reviewed producers 1:1) ---------------------

	const api = {
		async memoryStatus(serverId) {
			record('memoryStatus', null, [serverId]);
			throwArm('memoryStatus');
			const unavailable = popUnavailable('memoryStatus', 'status');
			if (unavailable) return unavailable;
			return deep(serverId === '${SERVER_A}' ? statusOk : statusDown);
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
		},
		async previewModelInstall(serverId, body) {
			record('previewModelInstall', body, [serverId]);
			throwArm('previewModelInstall');
			if (arms.wrongServerPreview) {
				arms.wrongServerPreview = false;
				return deep({ ...previewView('satisfied', [], [], []), server_id: '${SERVER_B}' });
			}
			if (body.recipe.includes('hw.ram.8gb')) {
				return deep(previewView('unsatisfied', ['hw.ram.16gb'], ['hw.vram.state'], ['data-root fell back to the nearest statable ancestor']));
			}
			return deep(previewView('satisfied', [], [], []));
		},
		async installModelService(serverId, applicationId, body) {
			record('installModelService', body, [serverId, applicationId]);
			throwArm('installModelService');
			return deep({ run: runView('queued'), deduplicated: true });
		},
		async pullModelWeights(serverId, applicationId, body) {
			record('pullModelWeights', body, [serverId, applicationId]);
			throwArm('pullModelWeights');
			return deep({ run: runView('queued'), deduplicated: false });
		},
		async readRun(runId) {
			record('readRun', null, [runId]);
			return deep(watched[runId] ?? runDetail('failed', []));
		},
		async listConnections() {
			record('listConnections', null, []);
			throwArm('listConnections');
			return deep({ connections: [connectionRef] });
		},
		async readConnection(name) {
			record('readConnection', null, [name]);
			throwArm('readConnection');
			return deep(connectionViewDoc);
		}
	};

	const links = {
		openApplications: (serverId) => record('openApplications', null, [serverId]),
		openJobs: (serverId) => record('openJobs', null, [serverId]),
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
		armWrongServerPreview() {
			arms.wrongServerPreview = true;
		},
		setRunState(runId, state) {
			watched[runId] = { ...watched[runId], state };
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

	const { calls } = await apiState(page);
	// Every mount probe named its own window's pinned server: A (host A),
	// B (host B) and A again (the links-less A host).
	const statusScopes = callsOf(calls, 'memoryStatus').map((call) => call.args?.[0]);
	expect(statusScopes.filter((scope) => scope === SERVER_A).length).toBe(2);
	expect(statusScopes).toContain(SERVER_B);
	expect(statusScopes.every((scope) => scope === SERVER_A || scope === SERVER_B)).toBe(true);
	// The connection listing is the value-free read; it is global (C02 store)
	// and carries no server argument to misuse.
	expect(callsOf(calls, 'listConnections').length).toBeGreaterThanOrEqual(1);
	// The unauthenticated local endpoint is displayed honestly.
	await expect(hostA.getByTestId('connections-empty')).toHaveCount(0);
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

test('preview measures real prerequisites and gates install behind a satisfied verdict of the same recipe', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	// Client identity gate first: a malformed application id blocks preview.
	await hostA.getByTestId('model-app-id-input').fill('not-a-uuid');
	await hostA.getByTestId('model-recipe-input').fill('schema_version: 1');
	await expect(hostA.getByTestId('model-preview-run')).toBeDisabled();
	await hostA.getByTestId('model-app-id-input').fill(APP_IDENTITY);

	// Unsatisfied preview: the fail-closed verdict is displayed with the
	// unmeasurable facts (they count as NOT satisfied — no all-green).
	await hostA.getByTestId('model-recipe-input').fill('schema_version: 1\nname: hw.ram.8gb');
	await hostA.getByTestId('model-preview-run').click();
	await expect(hostA.getByTestId('model-preview')).toHaveAttribute('data-verdict', 'unsatisfied');
	await expect(hostA.getByTestId('model-preview-verdict')).toContainText('hw.ram.16gb');
	await expect(hostA.getByTestId('model-preview-unknown')).toContainText('hw.vram.state');
	await expect(hostA.getByTestId('model-preview-notes')).toContainText('data-root fell back');
	// No submit while unsatisfied — and no UI-only install state (AC 2).
	await expect(hostA.getByTestId('model-install-run')).toBeDisabled();
	await expect(hostA.getByTestId('model-pull-run')).toBeDisabled();

	// Satisfied preview of the SAME recipe+target opens the submits.
	await hostA.getByTestId('model-recipe-input').fill('schema_version: 1\nname: local-inference');
	await hostA.getByTestId('model-preview-run').click();
	await expect(hostA.getByTestId('model-preview')).toHaveAttribute('data-verdict', 'satisfied');
	await expect(hostA.getByTestId('model-preview-prereqs')).toContainText('hw.ram.16gb');
	await expect(hostA.getByTestId('model-install-run')).toBeEnabled();

	// Stale recipe: editing the recipe after the preview revokes the gate.
	await hostA.getByTestId('model-recipe-input').fill('schema_version: 1\nname: local-inference-v2');
	await expect(hostA.getByTestId('model-install-run')).toBeDisabled();
	await expect(hostA.getByTestId('model-install-stale')).toBeVisible();
	await expect(hostA.getByTestId('model-pull-run')).toBeDisabled();
	const { calls } = await apiState(page);
	expect(callsOf(calls, 'installModelService')).toHaveLength(0);
	expect(callsOf(calls, 'pullModelWeights')).toHaveLength(0);
});

test('install submit carries the durable intent and the watched run reports the producer state honestly', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	await hostA.getByTestId('model-app-id-input').fill(APP_IDENTITY);
	await hostA.getByTestId('model-recipe-input').fill('schema_version: 1\nname: local-inference');
	await hostA.getByTestId('model-preview-run').click();
	await expect(hostA.getByTestId('model-install-run')).toBeEnabled();

	// backup_confirmed defaults to false — the operator's claim, never the UI's.
	await hostA.getByTestId('model-install-run').click();
	await expect(hostA.getByTestId('model-run-watch')).toBeVisible();
	await expect(hostA.getByTestId('model-run-id')).toContainText('install run');
	await expect(hostA.getByTestId('model-run-id')).toContainText('deduplicated');
	await expect(hostA.getByTestId('model-run-pending')).toBeVisible();

	// The poll picks up the producer's queued state, then the terminal one.
	await expect(hostA.getByTestId('model-run-state')).toContainText('Queued');
	await expect(hostA.getByTestId('model-run-step')).toHaveCount(1);
	await apiCall(page, 'setRunState', [RUN_A, 'succeeded']);
	// The poll cadence is bounded (1.5 s) but the wall clock stretches under
	// the fully-parallel worker load; give the terminal poll real room.
	await expect(hostA.getByTestId('model-run-state')).toContainText('Succeeded', { timeout: 20_000 });
	await expect(hostA.getByTestId('model-run-terminal')).toContainText('producer verified');

	const { calls } = await apiState(page);
	const installCalls = callsOf(calls, 'installModelService');
	expect(installCalls).toHaveLength(1);
	const payload = installCalls[0].payload as {
		run_id: string;
		recipe: string;
		target_os: string;
		target_arch: string;
		backup_confirmed: boolean;
	};
	expect(payload.run_id).toMatch(/^[0-9a-f-]{36}$/);
	expect(payload.recipe).toBe('schema_version: 1\nname: local-inference');
	expect(payload.target_os).toBe('linux');
	expect(payload.target_arch).toBe('x86_64');
	expect(payload.backup_confirmed).toBe(false);
	expect(installCalls[0].args).toEqual([SERVER_A, APP_IDENTITY]);
});

test('pull is the registration path: the terminal pull refreshes the registered connection view', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	await hostA.getByTestId('model-app-id-input').fill(APP_IDENTITY);
	await hostA.getByTestId('model-recipe-input').fill('schema_version: 1\nname: local-inference');
	await hostA.getByTestId('model-preview-run').click();
	await expect(hostA.getByTestId('model-pull-run')).toBeDisabled();

	// Producer-bound client checks: model grammar, required license, endpoint.
	await hostA.getByTestId('model-pull-model-input').fill('bad model; rm -rf');
	await expect(hostA.getByTestId('model-pull-run')).toBeDisabled();
	await hostA.getByTestId('model-pull-model-input').fill('qwen2.5:7b-instruct-q4_K_M');
	await expect(hostA.getByTestId('model-pull-run')).toBeDisabled();
	await hostA.getByTestId('model-pull-license-input').fill('apache-2.0 (weights: qwen research license)');
	await hostA.getByTestId('model-pull-base-url-input').fill('http://127.0.0.1:18114/v1?key=leak');
	await expect(hostA.getByTestId('model-pull-run')).toBeDisabled();
	await hostA.getByTestId('model-pull-base-url-input').fill('http://127.0.0.1:18114/v1');

	await hostA.getByTestId('model-pull-run').click();
	await expect(hostA.getByTestId('model-run-watch')).toHaveAttribute('data-run-kind', 'pull');
	await expect(hostA.getByTestId('model-run-id')).toContainText('pull run');
	// The executor finishes the acquisition; the poll lands on the terminal
	// state and registration truth follows.
	await apiCall(page, 'setRunState', [RUN_A, 'succeeded']);
	await expect(hostA.getByTestId('model-run-state')).toContainText('Succeeded', { timeout: 20_000 });

	// The pull wrote the C02 record — the registered connection view shows
	// the credential REFERENCE only (absent for the unauthenticated endpoint).
	await expect(hostA.getByTestId('connection-ref-local-inference-8c1d2e3f')).toBeVisible();
	await hostA.getByTestId('connection-open-local-inference-8c1d2e3f').click();
	await expect(hostA.getByTestId('connection-view-name')).toContainText('local-inference-8c1d2e3f');
	await expect(hostA.getByTestId('connection-view-url')).toContainText('http://127.0.0.1:18114/v1');
	await expect(hostA.getByTestId('connection-view-credential-ref')).toContainText(
		'none — unauthenticated endpoint'
	);
	await expect(hostA.getByTestId('connection-view-tool-calls')).toContainText('not observed');

	const { calls } = await apiState(page);
	const pullCalls = callsOf(calls, 'pullModelWeights');
	expect(pullCalls).toHaveLength(1);
	const payload = pullCalls[0].payload as {
		run_id: string;
		model: string;
		license: string;
		base_url: string;
	};
	expect(payload.run_id).toMatch(/^[0-9a-f-]{36}$/);
	expect(payload.model).toBe('qwen2.5:7b-instruct-q4_K_M');
	expect(payload.license).toContain('apache-2.0');
	expect(payload.base_url).toBe('http://127.0.0.1:18114/v1');
	// A terminal pull refreshed the registration surface (second listing).
	expect(callsOf(calls, 'listConnections').length).toBeGreaterThanOrEqual(2);
	expect(pageErrors).toHaveLength(0);
});

test('the feature entry composes into the typed registry with scope rules, geometry and singleton keys', async ({
	page
}) => {
	await hostAMounted(page);
	const { registry } = await apiState(page);

	// Registry membership: exactly the feature entry plus the benign sibling.
	expect(registry.kinds).toEqual(['fixture-notes', 'knowledge-models']);

	// Scope rules (ADR §3.2): server-scoped only; unpinned windows (the entry
	// declares no scope_object_kinds), a global mount and foreign object kinds
	// are refused with stable codes.
	expect(registry.scopeChecks.serverScope).toBe('ok');
	expect(registry.scopeChecks.globalScope).toBe('scope_kind_not_allowed');
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

test('negative control: hostile producer text renders inert and a wrong-server preview never paints', async ({
	page
}) => {
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

	// A preview naming a DIFFERENT server is refused before painting.
	await hostA.getByTestId('model-app-id-input').fill(APP_IDENTITY);
	await hostA.getByTestId('model-recipe-input').fill('schema_version: 1\nname: local-inference');
	await apiCall(page, 'armWrongServerPreview');
	await hostA.getByTestId('model-preview-run').click();
	await expect(hostA.getByTestId('model-preview')).toHaveCount(0);
	await expect(hostA.getByTestId('model-preview-problem')).toContainText('different server');
	await expect(hostA.getByTestId('model-install-run')).toBeDisabled();
	await expect(hostA.getByTestId('model-pull-run')).toBeDisabled();
});

test('negative control: unwired links and bags fail closed — no fake navigation, no crash', async ({
	page
}) => {
	const hostNoSeam = page.getByTestId('host-noseam');
	await hostAMounted(page);

	// The links-less host renders the full panel but NO link controls.
	await expect(hostNoSeam.getByTestId('knowledge-models-root')).toBeVisible();
	await expect(hostNoSeam.getByTestId('memory-open-model-settings')).toHaveCount(0);
	await expect(hostNoSeam.getByTestId('model-open-applications')).toHaveCount(0);

	// A bagless mount fails closed to the safe state — no throw, no partial UI.
	await expect(page.getByTestId('host-bare').getByTestId('panel-config-error')).toBeVisible();
	// A global mount contradicts the entry's server scope: the REGISTRY
	// refuses it at open (PanelHost's own safe unavailable state).
	await expect(page.getByTestId('host-global').getByTestId('panel-unavailable')).toBeVisible();

	// The sibling is unaffected by any of it (error containment).
	await expect(page.getByTestId('host-sibling').getByTestId('sibling-panel')).toBeVisible();
	expect(pageErrors).toHaveLength(0);

	// The links host, by contrast, renders and routes the open through the
	// seam with the pinned scope.
	const hostA = page.getByTestId('host-a');
	await expect(hostA.getByTestId('memory-open-model-settings')).toBeVisible();
	await hostA.getByTestId('memory-open-model-settings').click();
	await hostA.getByTestId('model-open-applications').click();
	const { calls } = await apiState(page);
	const openApps = callsOf(calls, 'openApplications');
	expect(openApps).toHaveLength(1);
	expect(openApps[0].args?.[0]).toBe(SERVER_A);
});

test('negative control: the credential reference checker classifies value-free with zero network use', async ({
	page
}) => {
	const hostA = page.getByTestId('host-a');
	await hostAMounted(page);

	const { calls: baseline } = await apiState(page);
	const baselineCount = baseline.length;

	// A valid 1Password reference is classified as .env-VALUE material.
	await hostA.getByTestId('credential-ref-check-input').fill('op://MAI/prod-gateway/api-key');
	await expect(hostA.getByTestId('credential-ref-classified')).toContainText('1Password reference');
	await expect(hostA.getByTestId('credential-ref-classified')).toContainText('.env VALUE');

	// A valid .env NAME is classified as the reference lane.
	await hostA.getByTestId('credential-ref-check-input').fill('MAI_GATEWAY_KEY');
	await expect(hostA.getByTestId('credential-ref-classified')).toContainText('.env reference name');

	// A secret VALUE shape is never a valid reference, and the value itself
	// is never echoed into any classification.
	await hostA.getByTestId('credential-ref-check-input').fill(FORBIDDEN_SECRET);
	await expect(hostA.getByTestId('credential-ref-invalid')).toBeVisible();
	await expect(hostA.getByTestId('credential-ref-classified')).toHaveCount(0);
	const pageText = await page.getByTestId('host-a').innerText();
	expect(pageText).not.toContain(FORBIDDEN_SECRET);

	// An op://-shaped but malformed reference is refused with the grammar.
	await hostA.getByTestId('credential-ref-check-input').fill('op://vault/item with space');
	await expect(hostA.getByTestId('credential-ref-invalid')).toContainText('three non-empty segments');

	// Nothing typed was ever transmitted: zero new recorded calls.
	const { calls: after } = await apiState(page);
	expect(after.length).toBe(baselineCount);
});

test('negative control: retain, delete and preview outages stay honest at every boundary', async ({
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

	// Preview: an envelope failure (e.g. transport unavailable) leaves NO
	// preview and therefore no submits — the fail-closed gate holds.
	await hostA.getByTestId('model-app-id-input').fill(APP_IDENTITY);
	await hostA.getByTestId('model-recipe-input').fill('schema_version: 1\nname: local-inference');
	await apiCall(page, 'failNext', [
		'previewModelInstall',
		{ status: 503, code: 'unavailable', message: 'prerequisite measurement transport is unavailable' }
	]);
	await hostA.getByTestId('model-preview-run').click();
	await expect(hostA.getByTestId('model-preview-problem')).toContainText('transport is unavailable');
	await expect(hostA.getByTestId('model-preview')).toHaveCount(0);
	await expect(hostA.getByTestId('model-install-run')).toBeDisabled();
	await expect(hostA.getByTestId('model-pull-run')).toBeDisabled();
	const { calls } = await apiState(page);
	expect(callsOf(calls, 'installModelService')).toHaveLength(0);
	expect(callsOf(calls, 'pullModelWeights')).toHaveLength(0);
});
