import { expect, test } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-06-web-models — real-browser proofs for the model-settings panel
 * (frontend/src/lib/panels/ModelSettingsPanel.svelte) and its feature-owned
 * registry entry (frontend/src/lib/workspace/entries/06-web-models.ts).
 *
 * Focused per the test policy: the panel is exercised in real Chromium,
 * mounted through the reviewed window host (`PanelHost`) and registry with a
 * fixture in-page API implementing the panel's typed service shape (the same
 * interface `createMaiApiClient()` gains at desktop composition). The REAL
 * backend boundary — C01 envelope codes, 409 conflict/selection semantics,
 * producer field validation, session/origin/CSRF — is owned by `06-model-api`
 * tests against the real ASGI app; the assembled desktop journey (panel
 * beside other panels in the real shell, live provider round) is owned by
 * `06-verify` at the phase gate (AGT-PROVIDER).
 *
 * Harness (self-contained, following the server-profile/ui-controls
 * convention):
 *  1. test.beforeAll runs an IN-MEMORY Vite production build (write: false)
 *     of a virtual app that imports the real panel, the real feature entry,
 *     the real registry + PanelHost, and mounts five hosts: MAIN + SECOND on
 *     the global scope with the same services object (instance isolation),
 *     BARE with NO services (fail-closed), SERVER-SCOPED (a server-scope
 *     descriptor for a global-only entry — the host must refuse it) and one
 *     benign sibling panel (coexistence/no interference).
 *  2. Each test serves the built JS/CSS/HTML through Playwright route
 *     interception. The fixture API mirrors the reviewed producer semantics
 *     (`mai.api.models`): value-free references, `expected_revision` guard ⇒
 *     value-free 409 `conflict`, selection-named records refused for delete,
 *     capability-gated selection/tool probes ⇒ 422 `unsupported_option`, and
 *     the classified probe outcome vocabulary resolved from the recorded
 *     request body AFTER the in-flight gate (the body is pinned at click
 *     time — exactly what acceptance 2 requires the panel to guarantee).
 *     Credential material exists ONLY as the `credential_ref` NAME plus a
 *     never-displayed secret-shaped marker the "resolver" would return.
 */

const FIXTURE_URL = '/mai-models-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-models-app.svelte';
const ENTRY_ID = 'virtual:mai-models-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/ModelSettingsPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/06-web-models.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_PIN = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const WINDOW_MAIN = '6a6b7c8d-9e0f-4a1b-8c3d-4e5f6a7b8c9d';
const WINDOW_SECOND = '7b7c8d9e-0f1a-4b2c-9d4e-5f6a7b8c9d0e';
const WINDOW_BARE = '8c8d9e0f-1a2b-4c3d-8e5f-6a7b8c9d0e1f';
const WINDOW_SERVER = '9d9e0f1a-2b3c-4d4e-9f6a-7b8c9d0e1f2a';
const WINDOW_SIBLING = 'a0af1b2c-3d4e-4e5f-8a7b-8c9d0e1f2a3b';
const RUN_PIN = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';

/**
 * Secret-SHAPED fixture markers (synthetic values, NOT secrets): the value
 * the credential reference WOULD resolve to, and a canary endpoint body —
 * neither may EVER appear in the DOM or in any panel-issued payload
 * (C03 rule 6 / brief acceptance 1).
 */
const FIXTURE_SECRET = 'sk-fixture-06webmodels-NEVER-DISPLAY';
const FIXTURE_CREDENTIAL_REF = 'MAI_FIXTURE_MODEL_KEY';

/** The frozen stale-edit conflict message of the models API (models.py). */
const CONFLICT_MESSAGE = 'the record changed since the last observed revision';
/** The frozen selection-named-record delete refusal (models.py). */
const SELECTION_CONFLICT_MESSAGE = 'the stored selection names this record; re-point it first';

type ReadFailure = { status: number; code: string; message: string };

interface ModelsDb {
	connections: Record<string, Record<string, unknown>>;
	profiles: Record<string, Record<string, unknown>>;
	selection: {
		connection: string;
		profile: string;
		revision: number;
		schema_revision: number;
		sha256: string;
	} | null;
}

interface RegistryProbe {
	kinds: string[];
	checkScope(kind: string, scope: unknown): { ok: true } | { ok: false; code: string };
	defaultGeometry(kind: string): unknown;
	minGeometry(kind: string): unknown;
}

interface ModelsWindowApi {
	calls: Array<{ op: string; name?: string | null; payload?: unknown }>;
	db: ModelsDb;
	armListGate(): void;
	releaseListGate(): void;
	armProbeGate(): void;
	releaseProbeGate(): void;
	failNextRead(failure: ReadFailure | null): void;
	failNextWrite(failure: ReadFailure | null): void;
	setSelection(connection: string, profile: string): void;
	bumpConnection(name: string): void;
	toggleHost(host: 'main' | 'second' | 'bare'): void;
	registry: RegistryProbe;
}

declare global {
	interface Window {
		__maiModelsApi?: ModelsWindowApi;
		/** Set by a test init script: arm the list read gate at app boot. */
		__maiArmListGateOnBoot?: boolean;
		/** Set by a test init script: boot with NO stored selection (404). */
		__maiNoSelectionOnBoot?: boolean;
	}
}

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; fileName: string; isEntry: boolean; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

let harnessJs = '';
let harnessCss = '';

/**
 * The fixture app. Virtual .svelte modules compile in RUNES mode in this
 * in-memory build (observed convention), so the app uses `$state` only.
 */
const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { ApiError } from '${CLIENT}';
	import { modelSettingsEntry, modelSettingsServices } from '${ENTRY}';

	// --- fixture backend: the only writer; tests read it through window ------
	const CONFLICT = '${CONFLICT_MESSAGE}';
	const SELECTION_CONFLICT = '${SELECTION_CONFLICT_MESSAGE}';

	function clone(value) {
		return structuredClone(JSON.parse(JSON.stringify(value)));
	}

	function refs(records) {
		return Object.values(records).map((record) => ({
			name: record.name,
			revision: record.revision,
			schema_revision: 1,
			sha256: record.sha256
		}));
	}

	// One-shot failure injection: the armed failure applies to exactly the
	// next read/write and is then consumed (tests re-arm deliberately).
	function consumeReadFailure() {
		if (failNextRead) {
			const failure = failNextRead;
			failNextRead = null;
			throw new ApiError(failure.status, failure.code, failure.message);
		}
	}

	function consumeWriteFailure() {
		if (failNextWrite) {
			const failure = failNextWrite;
			failNextWrite = null;
			throw new ApiError(failure.status, failure.code, failure.message);
		}
	}

	function freshDb() {
		return {
			connections: {
				alpha: {
					schema_version: 1,
					name: 'alpha',
					base_url: 'https://fixture-gateway.example/v1',
					credential_ref: '${FIXTURE_CREDENTIAL_REF}',
					timeout_seconds: 20,
					headers: [{ name: 'X-Fixture-Header', value: 'fixture-metadata' }],
					capabilities: { tool_calls: true, reasoning: false },
					revision: 3,
					sha256: 'ab'.repeat(32)
				},
				beta: {
					schema_version: 1,
					name: 'beta',
					base_url: 'http://fixture-local:8080/v1',
					credential_ref: null,
					timeout_seconds: null,
					headers: [],
					capabilities: { tool_calls: false, reasoning: false },
					revision: 1,
					sha256: 'cd'.repeat(32)
				},
				// Probe-classification fixtures: connection names keyed to outcomes.
				dead: {
					schema_version: 1, name: 'dead', base_url: 'https://dead.example/v1',
					credential_ref: null, timeout_seconds: null, headers: [],
					capabilities: { tool_calls: false, reasoning: false }, revision: 1, sha256: 'ab'.repeat(32)
				},
				locked: {
					schema_version: 1, name: 'locked', base_url: 'https://locked.example/v1',
					credential_ref: 'MAI_LOCKED_KEY', timeout_seconds: null, headers: [],
					capabilities: { tool_calls: false, reasoning: false }, revision: 2, sha256: 'cd'.repeat(32)
				}
			},
			profiles: {
				'alpha-fast': {
					schema_version: 1,
					name: 'alpha-fast',
					connection: 'alpha',
					model: 'fixture-model-mini',
					temperature: 0.2,
					top_p: null,
					seed: null,
					max_tokens: 256,
					reasoning_effort: null,
					context_limit: null,
					output_limit: null,
					revision: 2,
					sha256: 'ef'.repeat(32)
				},
				'beta-deep': {
					schema_version: 1,
					name: 'beta-deep',
					connection: 'beta',
					model: 'fixture/deep-0',
					temperature: null,
					top_p: null,
					seed: 7,
					max_tokens: null,
					reasoning_effort: 'low',
					context_limit: 8192,
					output_limit: null,
					revision: 1,
					sha256: 'ba'.repeat(32)
				}
			},
			selection: window.__maiNoSelectionOnBoot === true ? null : {
				connection: 'alpha',
				profile: 'alpha-fast',
				revision: 5,
				schema_revision: 1,
				sha256: 'fe'.repeat(32)
			}
		};
	}
	const db = freshDb();
	const calls = [];
	let failNextRead = null;
	let failNextWrite = null;
	let listGate = null;
	let probeGate = null;

	const PROBE_OUTCOMES = {
		alpha: 'ok',
		beta: 'ok',
		dead: 'reachability',
		locked: 'auth'
	};

	const api = {
		async listConnections() {
			calls.push({ op: 'listConnections' });
			consumeReadFailure();
			if (listGate) await listGate.promise;
			return { connections: refs(db.connections) };
		},
		async readConnection(name) {
			calls.push({ op: 'readConnection', name });
			consumeReadFailure();
			const stored = db.connections[name];
			if (!stored) throw new ApiError(404, 'not_found', 'no such model record');
			return clone(stored);
		},
		async putConnection(name, payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'putConnection', name, payload: wire });
			consumeWriteFailure();
			const stored = db.connections[name];
			if (stored && payload.expected_revision !== stored.revision) {
				throw new ApiError(409, 'conflict', CONFLICT);
			}
			if (!stored && payload.expected_revision !== 0) {
				throw new ApiError(409, 'conflict', CONFLICT);
			}
			const record = stored || { schema_version: 1, name, headers: [] };
			record.base_url = payload.base_url;
			record.credential_ref = payload.credential_ref ?? null;
			record.timeout_seconds = payload.timeout_seconds ?? null;
			record.headers = payload.headers ? Object.entries(payload.headers).map(([headerName, value]) => ({ name: headerName, value })) : [];
			record.capabilities = {
				tool_calls: payload.capabilities ? payload.capabilities.tool_calls === true : false,
				reasoning: payload.capabilities ? payload.capabilities.reasoning === true : false
			};
			record.revision = (record.revision || 0) + 1;
			record.sha256 = '11'.repeat(32);
			db.connections[name] = record;
			return { name, revision: record.revision, schema_revision: 1, sha256: record.sha256 };
		},
		async deleteConnection(name, expectedRevision) {
			calls.push({ op: 'deleteConnection', name, payload: { expected_revision: expectedRevision } });
			consumeWriteFailure();
			const stored = db.connections[name];
			if (!stored) throw new ApiError(404, 'not_found', 'no such model record');
			if (stored.revision !== expectedRevision) throw new ApiError(409, 'conflict', CONFLICT);
			if (db.selection && db.selection.connection === name) {
				throw new ApiError(409, 'conflict', SELECTION_CONFLICT);
			}
			delete db.connections[name];
			return undefined;
		},
		async listProfiles() {
			calls.push({ op: 'listProfiles' });
			consumeReadFailure();
			if (listGate) await listGate.promise;
			return { profiles: refs(db.profiles) };
		},
		async readProfile(name) {
			calls.push({ op: 'readProfile', name });
			consumeReadFailure();
			const stored = db.profiles[name];
			if (!stored) throw new ApiError(404, 'not_found', 'no such model record');
			return clone(stored);
		},
		async putProfile(name, payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'putProfile', name, payload: wire });
			consumeWriteFailure();
			const stored = db.profiles[name];
			if (stored && payload.expected_revision !== stored.revision) {
				throw new ApiError(409, 'conflict', CONFLICT);
			}
			if (!stored && payload.expected_revision !== 0) {
				throw new ApiError(409, 'conflict', CONFLICT);
			}
			const record = stored || { schema_version: 1, name };
			record.connection = payload.connection;
			record.model = payload.model;
			record.temperature = payload.temperature ?? null;
			record.top_p = payload.top_p ?? null;
			record.seed = payload.seed ?? null;
			record.max_tokens = payload.max_tokens ?? null;
			record.reasoning_effort = payload.reasoning_effort ?? null;
			record.context_limit = payload.context_limit ?? null;
			record.output_limit = payload.output_limit ?? null;
			record.revision = (record.revision || 0) + 1;
			record.sha256 = '22'.repeat(32);
			db.profiles[name] = record;
			return { name, revision: record.revision, schema_revision: 1, sha256: record.sha256 };
		},
		async deleteProfile(name, expectedRevision) {
			calls.push({ op: 'deleteProfile', name, payload: { expected_revision: expectedRevision } });
			consumeWriteFailure();
			const stored = db.profiles[name];
			if (!stored) throw new ApiError(404, 'not_found', 'no such model record');
			if (stored.revision !== expectedRevision) throw new ApiError(409, 'conflict', CONFLICT);
			if (db.selection && db.selection.profile === name) {
				throw new ApiError(409, 'conflict', SELECTION_CONFLICT);
			}
			delete db.profiles[name];
			return undefined;
		},
		async readSelection() {
			calls.push({ op: 'readSelection' });
			consumeReadFailure();
			if (!db.selection) throw new ApiError(404, 'not_found', 'no selection stored');
			return clone(db.selection);
		},
		async putSelection(payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'putSelection', payload: wire });
			consumeWriteFailure();
			const connection = db.connections[payload.connection];
			const profile = db.profiles[payload.profile];
			if (!connection || !profile) throw new ApiError(404, 'not_found', 'no such model record');
			// Producer capability gate (models.py): a reasoning_effort setting on
			// a connection that does not declare reasoning is a classified 422.
			if (profile.reasoning_effort !== null && connection.capabilities.reasoning !== true) {
				throw new ApiError(422, 'validation_failed', 'model configuration document failed validation', '', { profile: 'unsupported_option' });
			}
			const current = db.selection;
			if (current && payload.expected_revision !== current.revision) {
				throw new ApiError(409, 'conflict', CONFLICT);
			}
			if (!current && payload.expected_revision !== 0) {
				throw new ApiError(409, 'conflict', CONFLICT);
			}
			db.selection = {
				connection: payload.connection,
				profile: payload.profile,
				revision: (current ? current.revision : 0) + 1,
				schema_revision: 1,
				sha256: '33'.repeat(32)
			};
			return clone(db.selection);
		},
		async deleteSelection(expectedRevision) {
			calls.push({ op: 'deleteSelection', payload: { expected_revision: expectedRevision } });
			consumeWriteFailure();
			const current = db.selection;
			if (!current) throw new ApiError(404, 'not_found', 'no selection stored');
			if (expectedRevision !== current.revision) throw new ApiError(409, 'conflict', CONFLICT);
			db.selection = null;
			return undefined;
		},
		async probe(payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'probe', payload: wire });
			consumeWriteFailure();
			// The recorded BODY is the binding: resolution happens from it AFTER
			// the gate, so a selection mutated mid-flight cannot retarget the
			// round — and a panel that re-read the selection would need a second
			// call, which the tests count.
			if (probeGate) await probeGate.promise;
			const connectionName = payload.connection ?? (db.selection ? db.selection.connection : null);
			const profileName = payload.profile ?? (db.selection ? db.selection.profile : null);
			if (!connectionName || !profileName) {
				throw new ApiError(404, 'not_found', 'no selection stored');
			}
			const connection = db.connections[connectionName];
			const profile = db.profiles[profileName];
			if (!connection || !profile) throw new ApiError(404, 'not_found', 'no such model record');
			if (payload.tools === true && connection.capabilities.tool_calls !== true) {
				throw new ApiError(422, 'validation_failed', 'model configuration document failed validation', '', { tools: 'unsupported_option' });
			}
			const outcome = PROBE_OUTCOMES[connectionName] || 'provider';
			return {
				connection: connectionName,
				profile: profileName,
				model: profile.model,
				connection_revision: connection.revision,
				profile_revision: profile.revision,
				tools: payload.tools === true,
				outcome,
				latency_ms: 42
			};
		}
	};
	const services = modelSettingsServices(api);

	// --- registry: the REAL feature entry + one benign sibling ----------------
	const sibling = createRawSnippet(() => {
		return {
			render: () =>
				'<div class="mai-sibling" data-testid="sibling-panel">' +
				'<p>Sibling fixture panel — benign, no domain data.</p>' +
				'<button type="button" data-testid="sibling-interactions">Sibling interactions: 0</button>' +
				'</div>',
			setup: (root) => {
				const button = root.querySelector('button');
				let count = 0;
				const onClick = () => {
					count += 1;
					button.textContent = 'Sibling interactions: ' + count;
				};
				button.addEventListener('click', onClick);
				return () => button.removeEventListener('click', onClick);
			}
		};
	});
	const registryResult = createPanelRegistry([
		modelSettingsEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;

	// --- hosts: main + second (global, same services), bare, server-scoped ----
	let showMain = $state(true);
	let showSecond = $state(true);
	let showBare = $state(true);

	function descriptor(windowId, scope, z) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: 'model-settings',
			scope,
			geometry: { x: 8, y: 8, width: 680, height: 560 },
			presentation: 'normal',
			z,
			title_override: null
		};
	}
	const descriptorSibling = {
		schema_version: 1,
		window_id: '${WINDOW_SIBLING}',
		panel_kind: 'fixture-notes',
		scope: { kind: 'server', server_id: '${SERVER_PIN}' },
		geometry: { x: 16, y: 16, width: 320, height: 240 },
		presentation: 'normal',
		z: 5,
		title_override: null
	};

	const windowApi = {
		calls,
		db,
		armListGate() {
			const { promise, resolve } = Promise.withResolvers();
			listGate = { promise, resolve };
		},
		releaseListGate() {
			if (listGate) {
				listGate.resolve();
				listGate = null;
			}
		},
		armProbeGate() {
			const { promise, resolve } = Promise.withResolvers();
			probeGate = { promise, resolve };
		},
		releaseProbeGate() {
			if (probeGate) {
				probeGate.resolve();
				probeGate = null;
			}
		},
		failNextRead(failure) {
			failNextRead = failure;
		},
		failNextWrite(failure) {
			failNextWrite = failure;
		},
		setSelection(connection, profile) {
			// Another writer's default change (e.g. a chat submission's operator);
			// record documents are untouched by construction.
			const current = db.selection;
			db.selection = {
				connection,
				profile,
				revision: (current ? current.revision : 0) + 1,
				schema_revision: 1,
				sha256: '44'.repeat(32)
			};
		},
		bumpConnection(name) {
			db.connections[name].revision += 1;
		},
		toggleHost(host) {
			if (host === 'main') showMain = !showMain;
			else if (host === 'second') showSecond = !showSecond;
			else showBare = !showBare;
		},
		registry: {
			kinds: Array.from(registry.knownPanelKinds()).sort(),
			checkScope(kind, scope) {
				const result = registry.checkScope(kind, scope);
				return result.ok ? { ok: true } : { ok: false, code: result.issue.code };
			},
			defaultGeometry(kind) {
				return registry.defaultGeometry(kind);
			},
			minGeometry(kind) {
				return registry.minGeometry(kind);
			}
		}
	};
	window.__maiModelsApi = windowApi;
	if (window.__maiArmListGateOnBoot === true) windowApi.armListGate();
</script>

<section data-testid="host-main" aria-label="Host main">
	<ComponentHost label="Host main">
		{#if showMain}
			<PanelHost {registry} descriptor={descriptor('${WINDOW_MAIN}', { kind: 'global' }, 1)} services={services} />
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-second" aria-label="Host second">
	<ComponentHost label="Host second">
		{#if showSecond}
			<PanelHost {registry} descriptor={descriptor('${WINDOW_SECOND}', { kind: 'global' }, 2)} services={services} />
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-bare" aria-label="Host bare">
	<ComponentHost label="Host bare">
		{#if showBare}
			<PanelHost {registry} descriptor={descriptor('${WINDOW_BARE}', { kind: 'global' }, 3)} />
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-server-scoped" aria-label="Host server scoped">
	<ComponentHost label="Host server scoped">
		<PanelHost {registry} descriptor={descriptor('${WINDOW_SERVER}', { kind: 'server', server_id: '${SERVER_PIN}', object: { kind: 'run', id: '${RUN_PIN}' } }, 4)} services={services} />
	</ComponentHost>
</section>
<section data-testid="host-sibling" aria-label="Host sibling">
	<ComponentHost label="Host sibling">
		<PanelHost {registry} descriptor={descriptorSibling} />
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
		name: 'mai-models-harness',
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
			outDir: 'node_modules/.mai-models-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'models-[hash].js',
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
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI model settings fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"></script>
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
});

test('renders stored references, the selection, and credential-reference-only editing', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await page.goto(FIXTURE_URL);

	const main = page.getByTestId('host-main');

	// Value-free reference lists + the stored selection (C02 rule 4 views).
	await expect(main.getByTestId('connection-ref-alpha')).toHaveText('alpha');
	await expect(main.getByTestId('connection-ref-beta')).toHaveText('beta');
	await expect(main.getByTestId('profile-ref-alpha-fast')).toHaveText('alpha-fast');
	await expect(main.getByTestId('selection-view')).toContainText('alpha');
	await expect(main.getByTestId('selection-view')).toContainText('alpha-fast');
	await expect(main.getByTestId('selection-revision')).toHaveText('5');

	// The credential REFERENCE (a name) is displayable; the VALUE does not exist.
	await main.getByTestId('connection-open-alpha').click();
	const editor = main.getByTestId('connection-editor');
	await expect(editor.getByTestId('connection-name')).toHaveText('alpha');
	await expect(editor.getByLabel('Base URL')).toHaveValue('https://fixture-gateway.example/v1');
	await expect(editor.getByLabel('Credential reference')).toHaveValue(FIXTURE_CREDENTIAL_REF);
	await expect(editor.getByLabel('Timeout seconds')).toHaveValue('20');
	await expect(editor.getByTestId('connection-tool-calls')).toBeChecked();
	await expect(editor.getByTestId('connection-reasoning')).not.toBeChecked();

	// Beta declares NO capabilities — the protocol capability distinction is
	// visible per record (brief acceptance 1).
	await main.getByTestId('connection-open-beta').click();
	const betaEditor = main.getByTestId('connection-editor');
	await expect(betaEditor.getByTestId('connection-name')).toHaveText('beta');
	await expect(betaEditor.getByTestId('connection-tool-calls')).not.toBeChecked();
	await expect(betaEditor.getByTestId('connection-reasoning')).not.toBeChecked();

	// Acceptance 1: no credential VALUE anywhere in the rendered document; the
	// only credential-ish control is the reference text field (never opaque
	// value entry — there is no value to enter).
	const content = await page.content();
	expect(content).not.toContain(FIXTURE_SECRET);
	expect(content).not.toContain('credential_value');
	expect(content).not.toContain('api_key');
	await expect(page.locator('input[type="password"]')).toHaveCount(0);

	// Acceptance 3: the component arrived through the entry's real lazy loader.
	expect(servedChunks.length).toBeGreaterThan(0);

	expect(errors).toEqual([]);
});

test('connection save carries expected_revision, echoes headers, updates capabilities; create uses revision 0', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const main = page.getByTestId('host-main');
	const editor = main.getByTestId('connection-editor');

	await main.getByTestId('connection-open-alpha').click();
	await expect(editor.getByTestId('connection-name')).toHaveText('alpha');

	// Edit: declare the reasoning capability the producer gates at selection.
	await editor.getByTestId('connection-reasoning').check();
	await editor.getByTestId('connection-save').click();
	await expect(main.getByTestId('connection-saved')).toContainText('Saved connection "alpha"');

	const [call] = await page.evaluate(() =>
		window.__maiModelsApi!.calls.filter((entry) => entry.op === 'putConnection')
	);
	expect(call.name).toBe('alpha');
	const payload = call.payload as Record<string, unknown>;
	expect(payload).toMatchObject({
		base_url: 'https://fixture-gateway.example/v1',
		credential_ref: FIXTURE_CREDENTIAL_REF,
		timeout_seconds: 20,
		headers: { 'X-Fixture-Header': 'fixture-metadata' },
		capabilities: { tool_calls: true, reasoning: true },
		expected_revision: 3
	});
	expect(payload).not.toHaveProperty('api_key');
	expect(payload).not.toHaveProperty('credential_value');

	// Post-commit revision displayed; the listing caught up.
	await expect(editor.getByTestId('connection-revision')).toHaveText('4');
	await expect(main.getByTestId('connection-ref-alpha')).toContainText('alpha');

	// Create: name editable, revision 0 ⇒ stored document appears in the list.
	await main.getByTestId('connection-new').click();
	const created = main.getByTestId('connection-editor');
	await created.getByTestId('connection-name-input').fill('gamma');
	await created.getByLabel('Base URL').fill('https://gamma.example/v1');
	await created.getByLabel('Credential reference').fill('MAI_GAMMA_KEY');
	await created.getByTestId('connection-save').click();
	await expect(main.getByTestId('connection-saved')).toContainText('Saved connection "gamma"');
	await expect(main.getByTestId('connection-ref-gamma')).toBeVisible();

	const createCall = (
		await page.evaluate(() =>
			window.__maiModelsApi!.calls.filter((entry) => entry.op === 'putConnection')
		)
	)[1];
	expect((createCall.payload as Record<string, unknown>).expected_revision).toBe(0);
	const stored = await page.evaluate((name) => window.__maiModelsApi!.db.connections[name], 'gamma');
	expect(stored.credential_ref).toBe('MAI_GAMMA_KEY');
});

test('profile editor: allowlisted settings ride expected_revision; invalid model is blocked client-side', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const main = page.getByTestId('host-main');

	await main.getByTestId('profile-open-alpha-fast').click();
	const editor = main.getByTestId('profile-editor');
	await expect(editor.getByTestId('profile-name')).toHaveText('alpha-fast');
	await expect(editor.getByTestId('profile-connection-select')).toHaveValue('alpha');
	await expect(editor.getByLabel('Model')).toHaveValue('fixture-model-mini');
	await expect(editor.getByLabel('Temperature')).toHaveValue('0.2');
	await expect(editor.getByLabel('Max tokens')).toHaveValue('256');

	// Client-side mirror of the producer model-id rule: no call is made.
	await editor.getByLabel('Model').fill('bad model!');
	await expect(editor.getByLabel('Model')).toHaveAttribute('aria-invalid', 'true');
	await expect(editor.getByTestId('profile-save')).toBeDisabled();
	const writesBefore = await page.evaluate(
		() => window.__maiModelsApi!.calls.filter((entry) => entry.op === 'putProfile').length
	);
	expect(writesBefore).toBe(0);

	// Fix the identifier, set a reasoning effort, save.
	await editor.getByLabel('Model').fill('fixture-model-large');
	await editor.getByTestId('profile-reasoning-select').selectOption('low');
	await editor.getByTestId('profile-save').click();
	await expect(main.getByTestId('profile-saved')).toContainText('Saved profile "alpha-fast"');

	const [call] = await page.evaluate(() =>
		window.__maiModelsApi!.calls.filter((entry) => entry.op === 'putProfile')
	);
	expect(call.name).toBe('alpha-fast');
	expect(call.payload).toMatchObject({
		connection: 'alpha',
		model: 'fixture-model-large',
		temperature: 0.2,
		max_tokens: 256,
		reasoning_effort: 'low',
		seed: null,
		expected_revision: 2
	});
	await expect(editor.getByTestId('profile-revision')).toHaveText('3');
});

test('probe outcomes are classified; undeclared tool capability is a displayed producer rejection', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const main = page.getByTestId('host-main');

	// Defaults: the stored selection picks the pair; outcome ok.
	await main.getByTestId('probe-run').click();
	const outcome = main.getByTestId('probe-outcome');
	await expect(outcome).toHaveAttribute('data-outcome', 'ok');
	await expect(main.getByTestId('probe-pair')).toContainText('alpha');
	await expect(main.getByTestId('probe-pair')).toContainText('alpha-fast');
	await expect(main.getByTestId('probe-model')).toHaveText('fixture-model-mini');
	await expect(main.getByTestId('probe-revisions')).toHaveText('connection 3, profile 2');
	await expect(main.getByTestId('probe-tools-echo')).toHaveText('not requested');
	await expect(main.getByTestId('probe-latency')).toHaveText('42 ms');

	// auth: the endpoint rejected the credential (classified, not a crash).
	await main.getByTestId('probe-connection-select').selectOption('locked');
	await main.getByTestId('probe-run').click();
	await expect(main.getByTestId('probe-outcome')).toHaveAttribute('data-outcome', 'auth');
	await expect(main.getByTestId('probe-outcome-label')).toContainText('credential');

	// reachability: transport failure / deadline class.
	await main.getByTestId('probe-connection-select').selectOption('dead');
	await main.getByTestId('probe-run').click();
	await expect(main.getByTestId('probe-outcome')).toHaveAttribute('data-outcome', 'reachability');

	// Tool round on a connection that does NOT declare tool_calls: the
	// producer refuses with a classified 422; the panel displays it.
	await main.getByTestId('probe-connection-select').selectOption('beta');
	await main.getByTestId('probe-profile-select').selectOption('alpha-fast');
	await main.getByTestId('probe-tools').check();
	await main.getByTestId('probe-run').click();
	const problem = main.getByTestId('probe-error');
	await expect(problem).toBeVisible();
	await expect(problem).toHaveAttribute('data-error-code', 'validation_failed');
	await expect(main.getByTestId('probe-outcome')).toHaveCount(0);

	const probeCalls = await page.evaluate(() =>
		window.__maiModelsApi!.calls.filter((entry) => entry.op === 'probe')
	);
	expect(probeCalls).toHaveLength(4);
});

test('explicit binding: an in-flight probe keeps its pinned pair; re-selection never touches records or credentials', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const main = page.getByTestId('host-main');

	// Freeze the round AFTER the request leaves the panel.
	await page.evaluate(() => window.__maiModelsApi!.armProbeGate());
	await main.getByTestId('probe-connection-select').selectOption('alpha');
	await main.getByTestId('probe-profile-select').selectOption('alpha-fast');
	await main.getByTestId('probe-run').click();
	await expect(main.getByTestId('probe-run')).toBeDisabled(); // loading, no re-issue

	// While the round is in flight: change the DEFAULT through the panel.
	// beta-deep carries reasoning_effort and beta declares no reasoning, so the
	// producer-valid default becomes beta / alpha-fast; the invalid pair is
	// rejected with the classified 422 and DISPLAYED (C06 rule 1 gate).
	await main.getByTestId('selection-connection-select').selectOption('beta');
	await main.getByTestId('selection-profile-select').selectOption('beta-deep');
	await main.getByTestId('selection-apply').click();
	const selProblem = main.getByTestId('selection-problem');
	await expect(selProblem).toBeVisible();
	await expect(selProblem).toHaveAttribute('data-error-code', 'validation_failed');
	await expect(main.getByTestId('selection-view')).toContainText('alpha-fast'); // unchanged

	await main.getByTestId('selection-profile-select').selectOption('alpha-fast');
	await main.getByTestId('selection-apply').click();
	await expect(main.getByTestId('selection-saved')).toContainText('beta / alpha-fast');
	await expect(main.getByTestId('selection-revision')).toHaveText('6');

	// Release: the outcome still reports the PINNED pair and revisions.
	await page.evaluate(() => window.__maiModelsApi!.releaseProbeGate());
	const outcome = main.getByTestId('probe-outcome');
	await expect(outcome).toHaveAttribute('data-outcome', 'ok');
	await expect(main.getByTestId('probe-pair')).toContainText('alpha');
	await expect(main.getByTestId('probe-pair')).toContainText('alpha-fast');
	await expect(main.getByTestId('probe-revisions')).toHaveText('connection 3, profile 2');

	// Exactly ONE probe with the explicit names in the body — the panel never
	// re-read the changed default into the in-flight request (acceptance 2).
	const probeCalls = await page.evaluate(() =>
		window.__maiModelsApi!.calls.filter((entry) => entry.op === 'probe')
	);
	expect(probeCalls).toHaveLength(1);
	expect(probeCalls[0].payload).toEqual({ connection: 'alpha', profile: 'alpha-fast', tools: false });

	// The re-selection wrote ONLY selection records (one producer-refused 422
	// attempt + one commit): connection/profile documents — including the
	// credential reference — are untouched.
	const writes = await page.evaluate(() =>
		window.__maiModelsApi!.calls
			.filter((entry) => entry.op !== 'listConnections' && entry.op !== 'listProfiles' && entry.op !== 'readSelection' && entry.op !== 'probe')
			.map((entry) => entry.op)
	);
	expect(writes).toEqual(['putSelection', 'putSelection']);
	const alpha = await page.evaluate((name) => window.__maiModelsApi!.db.connections[name], 'alpha');
	expect(alpha.credential_ref).toBe(FIXTURE_CREDENTIAL_REF);
	expect(alpha.revision).toBe(3);
	const alphaFast = await page.evaluate(
		(name) => window.__maiModelsApi!.db.profiles[name],
		'alpha-fast'
	);
	expect(alphaFast.revision).toBe(2);
});

test('stale writes display the conflict and never overwrite; reload recovers; selection-named delete is refused', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const main = page.getByTestId('host-main');
	const editor = main.getByTestId('connection-editor');

	await main.getByTestId('connection-open-alpha').click();
	await expect(editor.getByTestId('connection-revision')).toHaveText('3');

	// Another writer bumps the record under this window.
	await page.evaluate((name) => window.__maiModelsApi!.bumpConnection(name), 'alpha');

	await editor.getByLabel('Base URL').fill('https://renamed.example/v1');
	await editor.getByTestId('connection-save').click();
	const banner = main.getByTestId('connection-conflict');
	await expect(banner).toBeVisible();
	await expect(banner).toContainText(CONFLICT_MESSAGE);

	// DISPLAYED, not overwritten: stored record untouched, input kept, save blocked.
	const stored = await page.evaluate((name) => window.__maiModelsApi!.db.connections[name], 'alpha');
	expect(stored.base_url).toBe('https://fixture-gateway.example/v1');
	expect(stored.revision).toBe(4);
	await expect(editor.getByLabel('Base URL')).toHaveValue('https://renamed.example/v1');
	await expect(main.getByTestId('connection-save')).toBeDisabled();

	// The explicit reload is the only path back to editing.
	await main.getByTestId('connection-reload').click();
	await expect(editor.getByTestId('connection-revision')).toHaveText('4');
	await expect(editor.getByLabel('Base URL')).toHaveValue('https://fixture-gateway.example/v1');
	await editor.getByLabel('Base URL').fill('https://renamed.example/v1');
	await editor.getByTestId('connection-save').click();
	await expect(main.getByTestId('connection-saved')).toContainText('Saved connection "alpha"');
	await expect(editor.getByTestId('connection-revision')).toHaveText('5');

	// Deleting the selection-named connection is refused (no dangling selection).
	await main.getByTestId('connection-delete').click();
	const deleteBanner = main.getByTestId('connection-conflict');
	await expect(deleteBanner).toContainText(SELECTION_CONFLICT_MESSAGE);
	const stillThere = await page.evaluate(
		(name) => window.__maiModelsApi!.db.connections[name] !== undefined,
		'alpha'
	);
	expect(stillThere).toBe(true);
});

test('mount reads only; close submits nothing; instances share nothing; sibling drives no traffic', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const main = page.getByTestId('host-main');
	const second = page.getByTestId('host-second');
	await expect(main.getByTestId('connection-ref-alpha')).toBeVisible();
	await expect(second.getByTestId('connection-ref-alpha')).toBeVisible();

	// Two hosts ⇒ two list reads + two selection reads; zero writes on mount.
	const bootOps = await page.evaluate(() =>
		window.__maiModelsApi!.calls.map((entry) => entry.op)
	);
	expect(bootOps.filter((op) => op === 'listConnections')).toHaveLength(2);
	expect(bootOps.filter((op) => op === 'listProfiles')).toHaveLength(2);
	expect(bootOps.filter((op) => op === 'readSelection')).toHaveLength(2);
	expect(
		bootOps.filter((op) => op.startsWith('put') || op.startsWith('delete') || op === 'probe')
	).toHaveLength(0);

	// Instance isolation: an editor open in MAIN does not appear in SECOND.
	await main.getByTestId('connection-open-alpha').click();
	await expect(main.getByTestId('connection-editor')).toBeVisible();
	await expect(second.getByTestId('connection-editor')).toHaveCount(0);

	// Local dirty edit + close (content unmount) submits nothing.
	await main.getByTestId('connection-editor').getByLabel('Base URL').fill('https://discarded.example/v1');
	await page.evaluate(() => window.__maiModelsApi!.toggleHost('main'));
	await expect(main.getByTestId('model-settings-root')).toHaveCount(0);
	let writes = await page.evaluate(() =>
		window.__maiModelsApi!.calls.filter((entry) => entry.op.startsWith('put') || entry.op.startsWith('delete'))
	);
	expect(writes).toHaveLength(0);
	const stored = await page.evaluate((name) => window.__maiModelsApi!.db.connections[name], 'alpha');
	expect(stored.base_url).toBe('https://fixture-gateway.example/v1');

	// Reopening re-reads the authoritative record; the local edit is gone.
	await page.evaluate(() => window.__maiModelsApi!.toggleHost('main'));
	await expect(main.getByTestId('connection-ref-alpha')).toBeVisible();
	await expect(main.getByTestId('connection-editor')).toHaveCount(0);

	// Interacting with the sibling panel changes nothing on the wire.
	const callsBefore = await page.evaluate(() => window.__maiModelsApi!.calls.length);
	const siblingButton = page.getByTestId('host-sibling').getByTestId('sibling-interactions');
	await siblingButton.click();
	await expect(siblingButton).toHaveText('Sibling interactions: 1');
	expect(await page.evaluate(() => window.__maiModelsApi!.calls.length)).toBe(callsBefore);
});

test('fail-closed: missing services and a server-scoped mount render safe states without requests', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	// BARE host: no services bag ⇒ visible config error, zero requests.
	await expect(page.getByTestId('host-bare').getByTestId('panel-config-error')).toBeVisible();

	// SERVER-SCOPED host: a global-only entry refuses the server/object scope
	// at the host (registry check) — the panel body never mounts.
	const scoped = page.getByTestId('host-server-scoped');
	await expect(scoped.getByTestId('panel-unavailable')).toHaveAttribute(
		'data-reason',
		'scope_kind_not_allowed'
	);
	await expect(scoped.getByTestId('model-settings-root')).toHaveCount(0);

	// Two usable hosts read; the bare/scoped hosts issued nothing.
	const readCount = await page.evaluate(
		() => window.__maiModelsApi!.calls.filter((entry) => entry.op === 'listConnections').length
	);
	expect(readCount).toBe(2);
});

test('feature entry validates in the real registry (global-only scope, geometry, singleton policy)', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const probe = await page.evaluate((pin) => {
		const registry = window.__maiModelsApi!.registry;
		return {
			kinds: registry.kinds,
			globalScope: registry.checkScope('model-settings', { kind: 'global' }),
			serverScope: registry.checkScope('model-settings', {
				kind: 'server',
				server_id: pin.server
			}),
			objectPin: registry.checkScope('model-settings', {
				kind: 'server',
				server_id: pin.server,
				object: { kind: 'run', id: pin.run }
			}),
			defaultGeometry: registry.defaultGeometry('model-settings'),
			minGeometry: registry.minGeometry('model-settings')
		};
	}, { server: SERVER_PIN, run: RUN_PIN });

	expect(probe.kinds).toContain('model-settings');
	expect(probe.globalScope).toEqual({ ok: true });
	// A global-only entry rejects EVERY server scope at the kind gate — with
	// and without an object pin (registry kind check precedes object kinds).
	expect(probe.serverScope).toEqual({ ok: false, code: 'scope_kind_not_allowed' });
	expect(probe.objectPin).toEqual({ ok: false, code: 'scope_kind_not_allowed' });
	expect(probe.defaultGeometry).toEqual({ x: 120, y: 88, width: 680, height: 520 });
	expect(probe.minGeometry).toEqual({ x: 0, y: 0, width: 480, height: 360 });
});

test('loading state holds until the lists arrive; read failure renders an honest error with retry', async ({
	page
}) => {
	await page.addInitScript(() => {
		window.__maiArmListGateOnBoot = true;
	});
	await page.goto(FIXTURE_URL);
	const main = page.getByTestId('host-main');
	await expect(main.getByTestId('models-loading')).toBeVisible();
	await page.evaluate(() => window.__maiModelsApi!.releaseListGate());
	await expect(main.getByTestId('connection-ref-alpha')).toBeVisible();

	// A failed read on the NEXT remount renders the error state (C08 rule 6).
	await page.evaluate(() =>
		window.__maiModelsApi!.failNextRead({
			status: 503,
			code: 'unavailable',
			message: 'store unavailable'
		})
	);
	await page.evaluate(() => window.__maiModelsApi!.toggleHost('second'));
	await page.evaluate(() => window.__maiModelsApi!.toggleHost('second'));
	const second = page.getByTestId('host-second');
	const error = second.getByTestId('models-error');
	await expect(error).toBeVisible();
	await expect(error).toHaveAttribute('data-error-code', 'unavailable');
	// The main host is unaffected (error containment).
	await expect(main.getByTestId('connection-ref-alpha')).toBeVisible();

	// Retry after the fault clears re-reads successfully.
	await second.getByTestId('models-retry').click();
	await expect(second.getByTestId('connection-ref-alpha')).toBeVisible();
});

test('no stored selection is an honest empty state; probes without names fail classified', async ({
	page
}) => {
	await page.addInitScript(() => {
		window.__maiNoSelectionOnBoot = true;
	});
	await page.goto(FIXTURE_URL);
	const main = page.getByTestId('host-main');

	await expect(main.getByTestId('selection-empty')).toBeVisible();
	await expect(main.getByTestId('selection-view')).toHaveCount(0);

	// Probe with no names and no selection: 404 displayed, no fake round.
	await main.getByTestId('probe-run').click();
	const problem = main.getByTestId('probe-error');
	await expect(problem).toBeVisible();
	await expect(problem).toHaveAttribute('data-error-code', 'not_found');
	await expect(main.getByTestId('probe-outcome')).toHaveCount(0);

	// Storing a selection from the honest empty state creates it (revision 0).
	await main.getByTestId('selection-connection-select').selectOption('alpha');
	await main.getByTestId('selection-profile-select').selectOption('alpha-fast');
	await main.getByTestId('selection-apply').click();
	await expect(main.getByTestId('selection-saved')).toContainText('alpha / alpha-fast');
	await expect(main.getByTestId('selection-revision')).toHaveText('1');
});
