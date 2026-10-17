import { expect, test, type Locator, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-09-web-plugin-forms — real-browser proofs for the plugin forms panel.
 *
 * The harness compiles the REAL feature panel (`PluginConfigForm.svelte`), the
 * REAL feature entry (`09-web-plugin-forms.ts`), the REAL panel host/registry
 * and the shared UI primitives into a throwaway fixture app served from
 * virtual modules — nothing hits disk and no real backend exists. The fixture
 * API mirrors the reviewed producers 1:1 (the `09-plugin-api` plugins surface
 * and the `09-skills` read surface); recorded calls and pre-armed responses
 * are the test-observed boundary (the reviewed `01-web` component-browser
 * fixture convention, not an imaginary backend).
 *
 * Covered acceptance surfaces: the schema-generated configuration form over
 * the supported keyword subset with explicit validation (invalid required
 * fields blocked, producer-shaped errors surfaced); the install / configure /
 * enable distinction; credential REFERENCES only (no value field exists
 * anywhere, inline credential keys refused); MCP server registration with the
 * frozen transport vocabulary and bounded discovery UX; explicit server grant
 * bindings (C03 rule 7); skills inspection with the declared-minus-granted
 * binding report; fail-closed service bags and scope; revision-checked
 * writes with honest conflict handling (no auto-retry).
 */

const FIXTURE_URL = '/mai-plugins-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-plugins-app.svelte';
const ENTRY_ID = 'virtual:mai-plugins-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/components/PluginConfigForm.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/09-web-plugin-forms.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const WINDOW_MAIN = '7a7a7a7a-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_NOSEAM = '8b8b8b8b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_BADBAG = '9c9c9c9c-9999-4222-8333-444455556666';
const WINDOW_SIBLING = '0d0d0d0d-9999-4222-8333-444455556666';

/** A value-shaped string that must NEVER appear anywhere on the page. */
const FORBIDDEN_SECRET = 'sk-live-9f2b7c1a-never-typed-anywhere';

/** Untruncated runtime-failure receipt: a page error is a harness defect. */
const pageErrors: string[] = [];

type Fail = { status: number; code: string; message: string };

interface PluginsWindowApi {
	calls: Array<{ op: string; payload?: unknown; args?: unknown[] }>;
	failNextEditConfig(failure: Fail | null): void;
	failNextDiscovery(failure: Fail | null): void;
	failNextEditBinding(failure: Fail | null): void;
	armDeferredSave(): void;
	releaseSave(triple: unknown): void;
	setConfigRevision(plugin: string, revision: number): void;
	setBindingRevision(plugin: string, serverId: string, revision: number): void;
	reseedWeatherSaved(): void;
	registry: {
		kinds: string[];
		scopeChecks: {
			globalScope: string;
			serverScope: string;
			unknownKind: string;
		};
		defaultGeometry: unknown;
		minGeometry: unknown;
		instanceKey: string;
	};
}

declare global {
	interface Window {
		__maiPluginsApi?: PluginsWindowApi;
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
	import { PLUGIN_FORMS_PANEL_KIND, pluginFormsEntry, pluginFormsServices } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';

	let tick = 0;

	// --- plugin store (the canonical documents; producer-shaped) --------------

	function weatherSchema() {
		return {
			plugin: 'weather',
			kind: 'native',
			config_schema: {
				type: 'object',
				properties: {
					location: { type: 'string', minLength: 1, maxLength: 64, title: 'Location' },
					units: { type: 'string', enum: ['metric', 'imperial'], default: 'metric' },
					retries: { type: 'integer', minimum: 0, maximum: 5, default: 1 },
					verbose: { type: 'boolean', default: false },
					tags: { type: 'array', items: { type: 'string', maxLength: 12 }, maxItems: 4 },
					metadata: { type: 'object' },
					fancy: { type: 'string', unevaluatedProperties: false }
				},
				required: ['location'],
				additionalProperties: false
			}
		};
	}

	function mcpSchema() {
		return {
			plugin: 'gitmcp',
			kind: 'mcp',
			config_schema: {
				type: 'object',
				properties: {
					transport: { type: 'string', enum: ['stdio', 'streamable_http'] },
					command: { type: 'string', minLength: 1, maxLength: 512 },
					args: { type: 'array', items: { type: 'string', maxLength: 512 }, maxItems: 32 },
					cwd: { type: 'string', minLength: 1, maxLength: 1024 },
					url: { type: 'string', minLength: 1, maxLength: 2048 },
					connect_timeout_s: { type: 'number', exclusiveMinimum: 0, maximum: 300 },
					call_timeout_s: { type: 'number', exclusiveMinimum: 0, maximum: 300 },
					max_tools: { type: 'integer', minimum: 1, maximum: 1024 }
				},
				required: ['transport'],
				additionalProperties: false
			}
		};
	}

	function weatherDoc() {
		return {
			plugin: 'weather',
			enabled: true,
			kind: 'native',
			credential_refs: ['WEATHER_API_KEY'],
			config: { location: 'Oslo', units: 'metric', retries: 1 },
			revision: 2,
			schema_revision: 1,
			sha256: 'a'.repeat(64),
			bindings: []
		};
	}

	function gitmcpDoc() {
		return {
			plugin: 'gitmcp',
			enabled: true,
			kind: 'mcp',
			credential_refs: [],
			config: {
				transport: 'streamable_http',
				url: 'http://10.0.0.9:8321/mcp',
				max_tools: 64
			},
			revision: 1,
			schema_revision: 1,
			sha256: 'b'.repeat(64),
			bindings: [
				{
					server_id: SERVER_A,
					capabilities: ['mcp.gitmcp.search_docs'],
					revision: 1,
					schema_revision: 1,
					sha256: 'c'.repeat(64)
				}
			]
		};
	}

	// Store shape: docs carry the full document + bindings kept separately so
	// binding writes have their own revisions.
	const store = {
		installed: ['weather', 'broken-native'],
		config: {
			weather: weatherDoc(),
			gitmcp: gitmcpDoc()
		},
		schemas: {
			weather: weatherSchema(),
			gitmcp: mcpSchema(),
			'broken-native': {
				plugin: 'broken-native',
				kind: 'native',
				config_schema: null,
				load_rejection: 'descriptor_invalid'
			},
			'uninstalled-thing': {
				plugin: 'uninstalled-thing',
				kind: 'uninstalled',
				config_schema: null
			}
		},
		bindings: {
			weather: {},
			gitmcp: {
				[SERVER_A]: { capabilities: ['mcp.gitmcp.search_docs'], revision: 1 }
			},
			'broken-native': {}
		}
	};

	function deep(value) {
		return JSON.parse(JSON.stringify(value));
	}

	function configuredView(plugin) {
		const doc = store.config[plugin];
		const bindings = Object.entries(store.bindings[plugin] || {}).map(([serverId, binding]) => ({
			server_id: serverId,
			capabilities: [...binding.capabilities],
			revision: binding.revision,
			schema_revision: 1,
			sha256: 'd'.repeat(64)
		}));
		return {
			plugin: plugin,
			enabled: doc.enabled,
			kind: doc.config && doc.config.transport ? 'mcp' : 'native',
			credential_refs: [...doc.credential_refs],
			config: deep(doc.config),
			revision: doc.revision,
			schema_revision: doc.schema_revision,
			sha256: doc.sha256,
			bindings: bindings
		};
	}

	function grantedPool() {
		const granted = new Set();
		for (const plugin of Object.keys(store.config)) {
			for (const binding of Object.values(store.bindings[plugin] || {})) {
				for (const capability of binding.capabilities) granted.add(capability);
			}
		}
		return Array.from(granted).sort();
	}

	// --- recorded calls + armed behavior -------------------------------------

	const calls = [];
	/**
	 * One-shot failure arms, consumed by the next call to their op: the slot
	 * is cleared at the call site (a helper that nulled its own PARAMETER
	 * would leave the arm set forever — the F05 trap; docs/apps precedent).
	 */
	const arms = { editConfig: null, discovery: null, editBinding: null };
	// Deferred-save gate: the gate must survive consumption, or the released
	// payload never reaches the panel and assertions turn vacuously green.
	let deferredSave = null;
	let pendingSave = null;

	function record(op, payload, args) {
		calls.push({ op: op, payload: payload === undefined ? null : deep(payload), args: args });
	}

	function throwArm(name) {
		const failure = arms[name];
		if (failure) {
			arms[name] = null;
			throw new ApiError(failure.status, failure.code, failure.message);
		}
	}

	const CREDENTIAL_TOKENS = [
		'password', 'passwd', 'secret', 'token', 'private_key', 'passphrase', 'api_key', 'access_key'
	];

	function hasInlineCredentialKey(value, depth) {
		if (depth > 24) return null;
		if (Array.isArray(value)) {
			for (const item of value) {
				const found = hasInlineCredentialKey(item, depth + 1);
				if (found !== null) return found;
			}
			return null;
		}
		if (value && typeof value === 'object') {
			for (const [key, item] of Object.entries(value)) {
				const lower = key.toLowerCase();
				if (CREDENTIAL_TOKENS.some((token) => lower.includes(token))) return key;
				const found = hasInlineCredentialKey(item, depth + 1);
				if (found !== null) return found;
			}
		}
		return null;
	}

	function checkDeclaredSchema(config, declared) {
		if (!declared) {
			return Object.keys(config).length === 0 ? null : 'plugin declares no config_schema';
		}
		for (const [key, value] of Object.entries(config)) {
			const property = declared.properties && declared.properties[key];
			if (!property) return 'config key ' + key + ' is not declared';
			if (property.type === 'integer' && (!Number.isInteger(value) || typeof value === 'boolean')) {
				return 'config.' + key + ' must be an integer';
			}
			if (property.maxLength && typeof value === 'string' && value.length > property.maxLength) {
				return 'config.' + key + ' exceeds maxLength';
			}
		}
		for (const name of declared.required || []) {
			const value = config[name];
			if (value === undefined || value === null || value === '') {
				return 'config.' + name + ' is required';
			}
		}
		return null;
	}

	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

	const pluginsApi = {
		async catalog() {
			record('catalog', null, []);
			return {
				installed: [...store.installed],
				configured: Object.keys(store.config).sort().map((plugin) => configuredView(plugin))
			};
		},
		async readConfig(plugin) {
			record('readConfig', null, [plugin]);
			const doc = store.config[plugin];
			if (!doc) throw new ApiError(404, 'not_found', 'No configuration stored for this plugin');
			return {
				plugin: plugin,
				enabled: doc.enabled,
				credential_refs: [...doc.credential_refs],
				config: deep(doc.config),
				revision: doc.revision,
				schema_revision: doc.schema_revision,
				sha256: doc.sha256
			};
		},
		async editConfig(plugin, payload) {
			record('editConfig', payload, [plugin]);
			throwArm('editConfig');
			if (deferredSave) {
				pendingSave = deferredSave;
				deferredSave = null;
				const released = await pendingSave.promise;
				return deep(released);
			}
			const doc = store.config[plugin];
			if (!doc) {
				if (payload.expected_revision !== 0) {
					throw new ApiError(409, 'conflict', 'stale expected revision');
				}
			} else if (payload.expected_revision !== doc.revision) {
				throw new ApiError(409, 'conflict', 'stale expected revision or canonical state requires recovery');
			}
			if (!plugin || !/^[a-z0-9][a-z0-9._-]{0,127}$/.test(plugin)) {
				throw new ApiError(422, 'validation_failed', 'plugin must be a lowercase slug');
			}
			const inline = hasInlineCredentialKey(payload.config, 0);
			if (inline !== null) {
				throw new ApiError(422, 'inline_credential_forbidden', 'config.' + inline + ': inline credential fields are forbidden');
			}
			let declared = null;
			if (payload.config && payload.config.transport) {
				declared = mcpSchema().config_schema;
			} else if (store.schemas[plugin]) {
				declared = store.schemas[plugin].config_schema;
			}
			const violation = checkDeclaredSchema(payload.config || {}, declared);
			if (violation !== null) {
				throw new ApiError(422, 'validation_failed', violation);
			}
			if (payload.credential_refs && payload.credential_refs.length > 16) {
				throw new ApiError(422, 'validation_failed', 'credential_refs: too many references');
			}
			if (!store.config[plugin]) {
				store.config[plugin] = {
					plugin: plugin,
					enabled: payload.enabled,
					kind: 'uninstalled',
					credential_refs: [...(payload.credential_refs || [])],
					config: deep(payload.config || {}),
					revision: 0,
					schema_revision: 1,
					sha256: 'e'.repeat(64)
				};
				store.bindings[plugin] = {};
			}
			const stored = store.config[plugin];
			stored.enabled = payload.enabled;
			stored.credential_refs = [...(payload.credential_refs || [])];
			stored.config = deep(payload.config || {});
			stored.revision = stored.revision + 1;
			stored.sha256 = 'f'.repeat(64);
			if (stored.config && stored.config.transport) stored.kind = 'mcp';
			return { revision: stored.revision, schema_revision: stored.schema_revision, sha256: stored.sha256 };
		},
		async removeConfig(plugin, expectedRevision) {
			record('removeConfig', expectedRevision, [plugin]);
			const doc = store.config[plugin];
			if (!doc) throw new ApiError(404, 'not_found', 'Nothing stored for this plugin');
			if (Object.keys(store.bindings[plugin] || {}).length > 0) {
				throw new ApiError(409, 'conflict', 'Grant bindings still exist for this plugin');
			}
			if (expectedRevision !== doc.revision) {
				throw new ApiError(409, 'conflict', 'stale expected revision');
			}
			delete store.config[plugin];
			return null;
		},
		async readSchema(plugin) {
			record('readSchema', null, [plugin]);
			const declared = store.schemas[plugin];
			if (!declared && !store.config[plugin]) {
				throw new ApiError(404, 'not_found', 'plugin is neither configured nor installed');
			}
			if (store.config[plugin] && store.config[plugin].config && store.config[plugin].config.transport) {
				return mcpSchema();
			}
			if (declared) return deep(declared);
			return { plugin: plugin, kind: 'uninstalled', config_schema: null };
		},
		async listBindings(plugin) {
			record('listBindings', null, [plugin]);
			const scope = store.bindings[plugin] || {};
			return Object.entries(scope).map(([serverId, binding]) => ({
				server_id: serverId,
				capabilities: [...binding.capabilities],
				revision: binding.revision,
				schema_revision: 1,
				sha256: 'd'.repeat(64)
			}));
		},
		async editBinding(plugin, serverId, payload) {
			record('editBinding', payload, [plugin, serverId]);
			throwArm('editBinding');
			if (!store.config[plugin]) {
				throw new ApiError(409, 'conflict', 'binding: connection_config_missing');
			}
			if (!UUID_RE.test(serverId)) {
				throw new ApiError(422, 'validation_failed', 'server_id must be a canonical UUID string');
			}
			const scope = store.bindings[plugin];
			const existing = scope[serverId];
			const expected = existing ? existing.revision : 0;
			if (payload.expected_revision !== expected) {
				throw new ApiError(409, 'conflict', 'stale expected revision');
			}
			if (!Array.isArray(payload.capabilities) || payload.capabilities.length === 0) {
				throw new ApiError(422, 'validation_failed', 'capabilities must grant at least one id');
			}
			scope[serverId] = { capabilities: [...payload.capabilities], revision: expected + 1 };
			return { revision: scope[serverId].revision, schema_revision: 1, sha256: '0'.repeat(64) };
		},
		async removeBinding(plugin, serverId, expectedRevision) {
			record('removeBinding', expectedRevision, [plugin, serverId]);
			const scope = store.bindings[plugin] || {};
			const existing = scope[serverId];
			if (!existing) throw new ApiError(404, 'not_found', 'No binding stored under this identity');
			if (expectedRevision !== existing.revision) {
				throw new ApiError(409, 'conflict', 'stale expected revision');
			}
			delete scope[serverId];
			return null;
		},
		async runDiscovery(plugin) {
			record('runDiscovery', null, [plugin]);
			throwArm('discovery');
			const doc = store.config[plugin];
			if (!doc) throw new ApiError(404, 'not_found', 'No configuration stored for this plugin');
			if (!doc.config || !doc.config.transport) {
				throw new ApiError(409, 'conflict', 'configured plugin is not an MCP server connection');
			}
			if (!doc.enabled) {
				throw new ApiError(409, 'conflict', 'plugin is disabled; enable it to discover');
			}
			const granted = new Set();
			for (const binding of Object.values(store.bindings[plugin] || {})) {
				for (const capability of binding.capabilities) granted.add(capability);
			}
			return {
				plugin: plugin,
				transport: doc.config.transport,
				tool_count: 3,
				registered: Array.from(granted).sort(),
				ungranted: ['mcp.gitmcp.fetch_docs'],
				stale_grants: ['mcp.gitmcp.old_tool'],
				rejections: [
					{ tool: 'mcp.gitmcp.bad', code: 'descriptor_invalid', detail: 'input_schema must be a JSON object' }
				],
				limit_exceeded: false
			};
		}
	};

	const skillsApi = {
		async catalog() {
			record('skills.catalog', null, []);
			return {
				skills: [
					{
						skill: 'deploy-runbook',
						title: 'Deploy runbook',
						description: 'Hostile probe <img src=x onerror="window.__maiXssFired = true">',
						requires_capabilities: ['shell.execute', 'mcp.gitmcp.fetch_docs'],
						references: [],
						package_sha256: '1'.repeat(64),
						total_bytes: 2048
					}
				],
				rejections: [{ skill: 'hostile-skill', code: 'manifest_invalid' }]
			};
		},
		async readSkill(skill) {
			record('skills.readSkill', null, [skill]);
			if (skill !== 'deploy-runbook') {
				throw new ApiError(404, 'not_found', 'no such skill package');
			}
			const pool = grantedPool();
			const requires = ['shell.execute', 'mcp.gitmcp.fetch_docs'];
			return {
				skill: skill,
				title: 'Deploy runbook',
				description: 'Bounded deployment walkthrough',
				requires_capabilities: requires,
				instructions: {
					name: 'SKILL.md',
					text: 'instructions',
					sha256: '2'.repeat(64),
					size_bytes: 1024
				},
				documents: [
					{ name: 'checks.md', text: 'checks', sha256: '3'.repeat(64), size_bytes: 512 }
				],
				package_sha256: '1'.repeat(64),
				total_bytes: 2048,
				granted_capability_pool: pool,
				missing_from_granted_pool: requires.filter((need) => !pool.includes(need))
			};
		}
	};

	const services = pluginFormsServices(pluginsApi, skillsApi);
	const servicesNoSkills = pluginFormsServices(pluginsApi, null);
	const servicesBadBag = pluginFormsServices({}, null);

	// --- registry: the REAL feature entry + one benign sibling ----------------
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
		pluginFormsEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;
	const entryCheck = definePanelEntry(pluginFormsEntry());
	if (!entryCheck.ok) throw new Error('feature entry rejected: ' + entryCheck.issue.code);

	// --- hosts ----------------------------------------------------------------

	function descriptor(windowId, scope, z, kind) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: kind ?? PLUGIN_FORMS_PANEL_KIND,
			scope: scope,
			geometry: { x: 8, y: 8, width: 920, height: 700 },
			presentation: 'normal',
			z: z,
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

	const windowApi = {
		calls: calls,
		failNextEditConfig(failure) {
			arms.editConfig = failure;
		},
		failNextDiscovery(failure) {
			arms.discovery = failure;
		},
		failNextEditBinding(failure) {
			arms.editBinding = failure;
		},
		armDeferredSave() {
			deferredSave = Promise.withResolvers();
		},
		releaseSave(triple) {
			const entry = pendingSave;
			pendingSave = null;
			deferredSave = null;
			if (entry) entry.resolve(JSON.parse(JSON.stringify(triple)));
		},
		setConfigRevision(plugin, revision) {
			if (store.config[plugin]) store.config[plugin].revision = revision;
		},
		setBindingRevision(plugin, serverId, revision) {
			const binding = (store.bindings[plugin] || {})[serverId];
			if (binding) binding.revision = revision;
		},
		reseedWeatherSaved() {
			store.config.weather = weatherDoc();
		},
		registry: {
			kinds: Array.from(registry.knownPanelKinds()).sort(),
			scopeChecks: {
				globalScope: checkCode(PLUGIN_FORMS_PANEL_KIND, { kind: 'global' }),
				serverScope: checkCode(PLUGIN_FORMS_PANEL_KIND, { kind: 'server', server_id: SERVER_A }),
				unknownKind: checkCode('not-a-panel', { kind: 'global' })
			},
			defaultGeometry: registry.defaultGeometry(PLUGIN_FORMS_PANEL_KIND),
			minGeometry: registry.minGeometry(PLUGIN_FORMS_PANEL_KIND),
			instanceKey: singletonInstanceKey(PLUGIN_FORMS_PANEL_KIND, entryCheck.value, {
				kind: 'global'
			})
		}
	};
	window.__maiPluginsApi = windowApi;
</script>

<section data-testid="host-main" aria-label="Host main">
	<ComponentHost label="Host main">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_MAIN}', { kind: 'global' }, 1)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-noseam" aria-label="Host without skills seam">
	<ComponentHost label="Host no-skills">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_NOSEAM}', { kind: 'global' }, 2)}
			services={servicesNoSkills}
		/>
	</ComponentHost>
</section>
<section data-testid="host-badbag" aria-label="Host with incomplete bag">
	<ComponentHost label="Host bad bag">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_BADBAG}', { kind: 'global' }, 3)}
			services={servicesBadBag}
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
`;

const ENTRY_SOURCE = `
import { mount } from 'svelte';
import App from '${APP_ID}';
import '${TOKENS}';
mount(App, { target: document.getElementById('app') });
`;

// ---------------------------------------------------------------------------
// Build the harness once per worker (same shape as the reviewed docs fixture)
// ---------------------------------------------------------------------------

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-plugins-harness',
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
			outDir: 'node_modules/.mai-plugins-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'plugins-[hash].js',
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
<title>MAI plugins fixture</title>
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
// Fixture-API boundary (serialized dispatch; same convention as docs/apps)
// ---------------------------------------------------------------------------

/** Call one fixture-api method inside the page; args must be serializable. */
async function apiCall(page: Page, method: string, args: unknown[] = []): Promise<void> {
	const pair: [string, unknown[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiPluginsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		// Dynamic dispatch by serialized name: the fixture's own typed API
		// viewed as a name→method table; the typeof guard still checks the
		// lookup before any call.
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

/** Snapshot the fixture's recorded calls and precomputed registry summary. */
async function apiState(page: Page): Promise<{
	calls: RecordedCall[];
	registry: PluginsWindowApi['registry'];
}> {
	return page.evaluate(() => {
		const value = window.__maiPluginsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		return { calls: value.calls, registry: value.registry };
	});
}

/** Test-only recorded-wire boundary: read the LAST recorded op payload. */
function lastCall(calls: RecordedCall[], op: string): RecordedCall {
	const found = calls.filter((call) => call.op === op);
	if (found.length === 0) throw new Error(`no recorded call for op ${op}`);
	return found[found.length - 1];
}

/** The main window mounted (lazy chunk fetched through the real loader). */
async function mainMounted(page: Page): Promise<Locator> {
	const hostMain = page.getByTestId('host-main');
	await expect(hostMain.getByTestId('plugins-root')).toBeVisible();
	await expect(hostMain.getByTestId('configured-list')).toBeVisible();
	expect(servedChunks.length).toBeGreaterThan(0);
	return hostMain;
}

// ---------------------------------------------------------------------------

test('the catalogue keeps installed, configured and enabled distinct beside a sibling; the entry passes the real registry', async ({
	page
}) => {
	const hostMain = await mainMounted(page);

	// The desktop holds the plugin panel BESIDE a sibling panel.
	await expect(page.getByTestId('host-sibling').getByTestId('sibling-panel')).toBeVisible();

	// Installed = distribution metadata, listed separately from documents.
	const installed = hostMain.getByTestId('installed-list').getByTestId('installed-name');
	await expect(installed).toHaveCount(2);
	await expect(hostMain.getByTestId('catalog-installed-count')).toHaveText('2 installed');

	// Configured documents carry kind, enabled flag, revision and grants —
	// three distinct facts, never merged.
	const rows = hostMain.getByTestId('configured-row');
	await expect(rows).toHaveCount(2);
	const weatherRow = rows.filter({ hasText: 'weather' });
	await expect(weatherRow.getByTestId('configured-kind')).toHaveText('native');
	await expect(weatherRow.getByTestId('configured-enabled')).toHaveText('enabled');
	await expect(weatherRow.getByTestId('configured-revision')).toHaveText('r2');
	await expect(weatherRow.getByTestId('configured-refs')).toHaveText('WEATHER_API_KEY');
	await expect(weatherRow.getByTestId('configured-bindings')).toHaveText('0');
	const gitRow = rows.filter({ hasText: 'gitmcp' });
	await expect(gitRow.getByTestId('configured-kind')).toHaveText('mcp');
	await expect(gitRow.getByTestId('configured-bindings')).toHaveText('1');

	// Registry contract through the REAL entry + registry: global scope only,
	// frozen geometry, singleton per scope.
	const { registry: summary } = await apiState(page);
	expect(summary.kinds).toEqual(['fixture-notes', 'plugin-forms']);
	expect(summary.scopeChecks.globalScope).toBe('ok');
	expect(summary.scopeChecks.serverScope).toBe('scope_kind_not_allowed');
	expect(summary.scopeChecks.unknownKind).toBe('unknown_panel_kind');
	expect(summary.defaultGeometry).toEqual({ x: 72, y: 64, width: 900, height: 640 });
	expect(summary.minGeometry).toEqual({ x: 0, y: 0, width: 480, height: 360 });
	expect(summary.instanceKey).toBe('plugin-forms::global');

	// Fail-closed bags: an incomplete plugins bag renders the honest state —
	// it never substitutes a fallback or renders a fake catalogue.
	await expect(page.getByTestId('host-badbag').getByTestId('plugins-unusable')).toBeVisible();
	await expect(page.getByTestId('host-badbag').getByTestId('configured-row')).toHaveCount(0);

	// Mount reads only: exactly the two catalogue reads of the two mounted
	// full-service windows, each naming no implicit target (global panel).
	const { calls } = await apiState(page);
	expect(calls.filter((call) => call.op === 'catalog').length).toBe(2);

	// Hostile skill text is rendered as text, never as a node (checked on the
	// skills tab below; here the catalogue stays the mount-read surface).
	expect(pageErrors).toEqual([]);
});

test('the generated form blocks an invalid required field, then persists an exact versioned document', async ({
	page
}) => {
	const hostMain = await mainMounted(page);

	// Open the generated form for the configured native plugin.
	await hostMain.getByTestId('configured-row').filter({ hasText: 'weather' }).getByTestId('configure-start').click();
	await expect(hostMain.getByTestId('schema-summary')).toContainText('7 properties');
	await expect(hostMain.getByTestId('generated-form')).toBeVisible();

	// Declared controls: enum select, integer input, checkbox, list textarea.
	const units = hostMain.getByTestId('field-units');
	await expect(units.locator('select')).toBeVisible();
	const retries = hostMain.getByTestId('field-retries');
	await expect(retries.locator('input[type=number]')).toHaveValue('1');
	await expect(hostMain.getByTestId('field-verbose').locator('input[type=checkbox]')).not.toBeChecked();

	// Stored values pre-fill the form from the stored document.
	const location = hostMain.getByTestId('field-location');
	await expect(location).toHaveValue('Oslo');

	// Invalid required field: clearing location and saving is BLOCKED
	// client-side with a per-field error; nothing is sent.
	await location.fill('');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.locator('#field-location-error')).toHaveText('required');
	await expect(hostMain.getByTestId('config-error')).toContainText('Fix the marked fields');
	let { calls } = await apiState(page);
	expect(calls.filter((call) => call.op === 'editConfig').length).toBe(0);

	// A bounded field blocks too: retries above the declared maximum.
	await retries.locator('input').fill('9');
	await location.fill('Bergen');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('field-error-retries')).toHaveText('≤ 5');
	calls = (await apiState(page)).calls;
	expect(calls.filter((call) => call.op === 'editConfig').length).toBe(0);

	// Valid edit: the full document travels — every field, the caller's
	// revision (stale writes refused), the reference names only.
	await retries.locator('input').fill('3');
	await units.locator('select').selectOption('imperial');
	await hostMain.getByTestId('field-verbose').locator('input[type=checkbox]').check();
	await hostMain.getByTestId('field-tags').locator('textarea').fill('temp\nwind');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('config-status')).toHaveText('Saved revision r3');

	const put = lastCall((await apiState(page)).calls, 'editConfig');
	expect(put.args).toEqual(['weather']);
	expect(put.payload).toEqual({
		expected_revision: 2,
		enabled: true,
		credential_refs: ['WEATHER_API_KEY'],
		config: {
			location: 'Bergen',
			units: 'imperial',
			retries: 3,
			verbose: true,
			tags: ['temp', 'wind']
		}
	});

	// The catalogue row now shows the new revision triple (r3).
	await hostMain.getByTestId('tab-catalog').click();
	await expect(
		hostMain.getByTestId('configured-row').filter({ hasText: 'weather' }).getByTestId('configured-revision')
	).toHaveText('r3');
	expect(pageErrors).toEqual([]);
});

test('refresh re-renders the masked configuration: references only, no value surface exists', async ({
	page
}) => {
	// Fresh page — the fixture store is seeded with the already-saved document
	// (the persisted state a refresh would read back).
	const hostMain = await mainMounted(page);
	await hostMain
		.getByTestId('configured-row')
		.filter({ hasText: 'weather' })
		.getByTestId('configure-start')
		.click();
	await expect(hostMain.getByTestId('generated-form')).toBeVisible();

	// The stored document re-renders into the generated controls.
	await expect(hostMain.getByTestId('field-location')).toHaveValue('Oslo');
	await expect(hostMain.getByTestId('field-units').locator('select')).toHaveValue('metric');
	await expect(hostMain.getByTestId('field-retries').locator('input[type=number]')).toHaveValue('1');

	// Credential REFERENCES render as .env names — and there is NO value
	// surface at all: no password input exists on the panel.
	const refs = hostMain.getByTestId('refs-editor');
	await expect(refs).toContainText('.env NAMES only, never values');
	await expect(hostMain.getByTestId('cred-ref-input').nth(0)).toHaveValue('WEATHER_API_KEY');
	await expect(hostMain.getByTestId('plugins-root').locator('input[type=password]')).toHaveCount(0);

	// The value-shaped string appears nowhere in the rendered page — the UI
	// never had a field for it, and the stored document carries only the name.
	const bodyText = await page.getByTestId('host-main').innerText();
	expect(bodyText).not.toContain(FORBIDDEN_SECRET);

	// A real refresh (new document + fresh fixture store, same seed) shows the
	// same masked configuration.
	await page.goto(FIXTURE_URL);
	const reloaded = page.getByTestId('host-main');
	await expect(reloaded.getByTestId('configured-list')).toBeVisible();
	await reloaded
		.getByTestId('configured-row')
		.filter({ hasText: 'weather' })
		.getByTestId('configure-start')
		.click();
	await expect(reloaded.getByTestId('field-location')).toHaveValue('Oslo');
	await expect(reloaded.getByTestId('cred-ref-input').nth(0)).toHaveValue('WEATHER_API_KEY');
	await expect(reloaded.getByTestId('plugins-root').locator('input[type=password]')).toHaveCount(0);
	expect(pageErrors).toEqual([]);
});

test('enable/disable is an explicit document edit; a disabled plugin refuses discovery', async ({
	page
}) => {
	const hostMain = await mainMounted(page);
	const gitRow = () => hostMain.getByTestId('configured-row').filter({ hasText: 'gitmcp' });

	// Disable: one explicit full-document edit at the observed revision.
	await gitRow().getByTestId('enable-toggle').click();
	await expect(hostMain.getByTestId('catalog-notice')).toHaveText('gitmcp disabled');
	await expect(gitRow().getByTestId('configured-enabled')).toHaveText('disabled');
	const put = lastCall((await apiState(page)).calls, 'editConfig');
	expect(put.args).toEqual(['gitmcp']);
	expect(put.payload).toEqual({
		expected_revision: 1,
		enabled: false,
		credential_refs: [],
		config: { transport: 'streamable_http', url: 'http://10.0.0.9:8321/mcp', max_tools: 64 }
	});

	// Discovery on the disabled server is REFUSED (the enabled tool surface is
	// gone) and the refusal is surfaced, never hidden behind a stale report.
	await gitRow().getByTestId('discovery-start').click();
	await hostMain.getByTestId('discovery-run').click();
	await expect(hostMain.getByTestId('discovery-error')).toContainText('plugin is disabled');

	// Re-enable: the document flips back at the new revision.
	await hostMain.getByTestId('tab-catalog').click();
	await gitRow().getByTestId('enable-toggle').click();
	await expect(hostMain.getByTestId('catalog-notice')).toHaveText('gitmcp enabled');
	await expect(gitRow().getByTestId('configured-enabled')).toHaveText('enabled');
	const reenable = lastCall((await apiState(page)).calls, 'editConfig');
	expect(reenable.payload).toMatchObject({ enabled: true, expected_revision: 2 });
	expect(pageErrors).toEqual([]);
});

test('the MCP registration template renders the frozen transport vocabulary for an unconfigured connection', async ({
	page
}) => {
	const hostMain = await mainMounted(page);

	// An unconfigured, uninstalled name declares no schema: the registration
	// path is explicit, not invented.
	await hostMain.getByTestId('tab-configure').click();
	await hostMain.getByTestId('configure-new-input').fill('gitmcp2');
	await hostMain.getByTestId('configure-new').getByRole('button').click();
	await expect(hostMain.getByTestId('mcp-register-note')).toBeVisible();
	await expect(hostMain.getByTestId('mcp-register-form')).toBeVisible();

	// streamable_http: the URL field is required; stdio-only fields stay out.
	await hostMain.getByTestId('mcp-transport').selectOption('streamable_http');
	await expect(hostMain.getByTestId('mcp-url')).toBeVisible();
	await expect(hostMain.getByTestId('mcp-command')).toHaveCount(0);

	// Missing URL: blocked with a per-field error, nothing sent.
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.locator('#mcp-url-field-error')).toHaveText(
		'required for streamable_http transport'
	);
	let { calls } = await apiState(page);
	expect(calls.filter((call) => call.op === 'editConfig').length).toBe(0);

	// stdio: command/args/cwd appear; url disappears.
	await hostMain.getByTestId('mcp-transport').selectOption('stdio');
	await expect(hostMain.getByTestId('mcp-command')).toBeVisible();
	await expect(hostMain.getByTestId('mcp-args')).toBeVisible();
	await expect(hostMain.getByTestId('mcp-cwd')).toBeVisible();
	await expect(hostMain.getByTestId('mcp-url')).toHaveCount(0);

	// Complete stdio registration persists and the catalogue row appears.
	await hostMain.getByTestId('mcp-command').fill('docker');
	await hostMain.getByTestId('mcp-args').fill('run\n--rm\ngitmcp');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('config-status')).toHaveText('Saved revision r1');
	const put = lastCall((await apiState(page)).calls, 'editConfig');
	expect(put.args).toEqual(['gitmcp2']);
	expect(put.payload).toEqual({
		expected_revision: 0,
		enabled: false,
		credential_refs: [],
		config: {
			transport: 'stdio',
			command: 'docker',
			args: ['run', '--rm', 'gitmcp']
		}
	});
	// The store now classifies the connection as mcp; the row shows it.
	await hostMain.getByTestId('tab-catalog').click();
	await expect(
		hostMain
			.getByTestId('configured-row')
			.filter({ hasText: 'gitmcp2' })
			.getByTestId('configured-kind')
	).toHaveText('mcp');
	calls = (await apiState(page)).calls;
	expect(calls.filter((call) => call.op === 'readSchema').length).toBeGreaterThanOrEqual(2);
	expect(pageErrors).toEqual([]);
});

test('credential references stay value-free; inline credential keys are refused with the path named', async ({
	page
}) => {
	const hostMain = await mainMounted(page);
	await hostMain.getByTestId('configured-row').filter({ hasText: 'weather' }).getByTestId('configure-start').click();
	await expect(hostMain.getByTestId('generated-form')).toBeVisible();

	// A non-.env-shape reference is refused before any write.
	await hostMain.getByTestId('cred-ref-add').click();
	const refInput = hostMain.getByTestId('cred-ref-input').nth(1);
	await refInput.fill('lowercase_bad');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('refs-error')).toContainText('.env names');
	let { calls } = await apiState(page);
	expect(calls.filter((call) => call.op === 'editConfig').length).toBe(0);

	// Duplicate references are refused.
	await refInput.fill('WEATHER_API_KEY');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('refs-error')).toContainText('duplicate credential reference');

	// The object-typed property renders as raw JSON (explicit fallback) and a
	// credential-token key inside it is refused with the key path named —
	// credentials resolve by reference, never inline (C03 rule 6).
	const metadata = hostMain.getByTestId('field-metadata');
	await expect(metadata).toContainText('raw JSON');
	await metadata.locator('textarea').fill('{"api_key": "x"}');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('config-inline-error')).toContainText(
		'inline credential fields are forbidden: config.metadata.api_key'
	);
	calls = (await apiState(page)).calls;
	expect(calls.filter((call) => call.op === 'editConfig').length).toBe(0);

	// Fix everything: a proper reference set and clean metadata saves.
	await refInput.fill('WEATHER_ALT_KEY');
	await metadata.locator('textarea').fill('{"region": "eu"}');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('config-status')).toHaveText('Saved revision r3');
	const put = lastCall((await apiState(page)).calls, 'editConfig');
	expect(put.payload).toMatchObject({
		credential_refs: ['WEATHER_API_KEY', 'WEATHER_ALT_KEY'],
		config: { metadata: { region: 'eu' } }
	});
	expect(pageErrors).toEqual([]);
});

test('the supported schema subset is explicit: bounds, list bounds, JSON fallback and a locked rejection', async ({
	page
}) => {
	const hostMain = await mainMounted(page);
	await hostMain.getByTestId('configured-row').filter({ hasText: 'weather' }).getByTestId('configure-start').click();
	await expect(hostMain.getByTestId('generated-form')).toBeVisible();

	// Array bounds: five tags exceed maxItems 4 — blocked with the bound.
	await hostMain.getByTestId('field-tags').locator('textarea').fill('a\nb\nc\nd\ne');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('field-error-tags')).toHaveText('allows at most 4 items');

	// Item bounds: a 13-character tag exceeds the item maxLength 12.
	await hostMain.getByTestId('field-tags').locator('textarea').fill('abcdefghijklm');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('field-error-tags')).toHaveText('items: must be at most 12 characters');

	// The unsupported keyword is displayed as an explicit note on the raw-JSON
	// editor — shown unmodified, and the producer will refuse the document.
	const fancy = hostMain.getByTestId('field-fancy');
	await expect(fancy).toContainText('raw JSON');
	await expect(fancy).toContainText('unsupported keyword');

	// Invalid JSON in the fallback editor is refused client-side.
	await hostMain.getByTestId('field-metadata').locator('textarea').fill('{broken');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('field-error-metadata')).toHaveText('must be valid JSON');

	// A plugin whose load was rejected has NO known schema: the form locks
	// with the value-free rejection code instead of inventing fields.
	await hostMain.getByTestId('configure-close').click();
	await expect(hostMain.getByTestId('configure-new-input')).toBeVisible();
	await hostMain.getByTestId('configure-new-input').fill('broken-native');
	await hostMain.getByTestId('configure-new').getByRole('button').click();
	await expect(hostMain.getByTestId('schema-rejection')).toContainText('descriptor_invalid');
	await expect(hostMain.getByTestId('generated-form')).toHaveCount(0);
	await expect(hostMain.getByTestId('config-save')).toHaveCount(0);
	expect(pageErrors).toEqual([]);
});

test('bounded discovery reports registered, ungranted, stale and rejected tools; failures stay surfaced', async ({
	page
}) => {
	const hostMain = await mainMounted(page);
	await hostMain.getByTestId('configured-row').filter({ hasText: 'gitmcp' }).getByTestId('discovery-start').click();

	// Happy path: exactly the granted subset registers; the rest is reported.
	await hostMain.getByTestId('discovery-run').click();
	await expect(hostMain.getByTestId('discovery-report')).toBeVisible();
	await expect(hostMain.getByTestId('discovery-transport')).toHaveText('streamable_http');
	await expect(hostMain.getByTestId('discovery-tool-count')).toHaveText('3 tools offered');
	await expect(hostMain.getByTestId('discovery-registered')).toHaveText('mcp.gitmcp.search_docs');
	await expect(hostMain.getByTestId('discovery-ungranted')).toHaveText('mcp.gitmcp.fetch_docs');
	await expect(hostMain.getByTestId('discovery-stale')).toHaveText('mcp.gitmcp.old_tool');
	await expect(hostMain.getByTestId('discovery-rejection')).toContainText('descriptor_invalid');

	// The diagnostic is bounded: a 502 from the producer is surfaced verbatim.
	await apiCall(page, 'failNextDiscovery', [
		{ status: 502, code: 'discovery_failed', message: 'bounded MCP discovery failed against the configured server' }
	]);
	await hostMain.getByTestId('discovery-run').click();
	await expect(hostMain.getByTestId('discovery-error')).toContainText('502 discovery_failed');

	// A native connection is never offered the MCP diagnostic: no fake
	// discovery action exists on non-MCP rows (the producer would refuse it).
	await hostMain.getByTestId('tab-catalog').click();
	const weatherRow = hostMain.getByTestId('configured-row').filter({ hasText: 'weather' });
	await expect(weatherRow.getByTestId('discovery-start')).toHaveCount(0);
	await expect(weatherRow.getByTestId('configured-kind')).toHaveText('native');
	expect(pageErrors).toEqual([]);
});

test('grant bindings are explicit, revision-checked and refuse a missing config; skills report missing grants', async ({
	page
}) => {
	const hostMain = await mainMounted(page);

	// Open the mcp connection's bindings: the stored grant lists first.
	await hostMain.getByTestId('configured-row').filter({ hasText: 'gitmcp' }).getByTestId('bindings-start').click();
	await expect(hostMain.getByTestId('binding-row')).toHaveCount(1);
	await expect(hostMain.getByTestId('binding-row').getByTestId('binding-server')).toHaveText(SERVER_A);
	await expect(hostMain.getByTestId('binding-row').getByTestId('binding-caps')).toHaveText('mcp.gitmcp.search_docs');

	// A NEW grant is an explicit write: the server UUID is typed here.
	await hostMain.getByTestId('binding-new').click();
	await hostMain.getByTestId('binding-server-input').fill('not-a-uuid');
	await hostMain.getByTestId('binding-caps-input').fill('mcp.gitmcp.fetch_docs');
	await hostMain.getByTestId('binding-save').click();
	await expect(hostMain.getByTestId('binding-error')).toContainText('canonical UUID');
	let { calls } = await apiState(page);
	expect(calls.filter((call) => call.op === 'editBinding').length).toBe(0);

	await hostMain.getByTestId('binding-server-input').fill(SERVER_B);
	await hostMain.getByTestId('binding-save').click();
	await expect(hostMain.getByTestId('binding-notice')).toContainText('Binding saved at r1');
	const put = lastCall((await apiState(page)).calls, 'editBinding');
	expect(put.args).toEqual(['gitmcp', SERVER_B]);
	expect(put.payload).toEqual({ expected_revision: 0, capabilities: ['mcp.gitmcp.fetch_docs'] });
	await expect(hostMain.getByTestId('binding-row')).toHaveCount(2);

	// Stale revision: the producer moved; the 409 is displayed and the panel
	// does NOT auto-retry with a fresh revision (exactly one recorded write).
	await hostMain.getByTestId('binding-row').filter({ hasText: SERVER_A }).getByTestId('binding-edit').click();
	await apiCall(page, 'setBindingRevision', ['gitmcp', SERVER_A, 7]);
	await hostMain.getByTestId('binding-caps-input').fill('mcp.gitmcp.search_docs\nmcp.gitmcp.fetch_docs');
	await hostMain.getByTestId('binding-save').click();
	await expect(hostMain.getByTestId('binding-error')).toContainText('409 conflict');
	calls = (await apiState(page)).calls;
	expect(calls.filter((call) => call.op === 'editBinding').length).toBe(2);

	// Revocation is revision-checked and names the server; the config stays.
	await hostMain.getByTestId('binding-row').filter({ hasText: SERVER_B }).getByTestId('binding-revoke').click();
	await expect(hostMain.getByTestId('revoke-confirm')).toContainText(SERVER_B);
	await hostMain.getByTestId('revoke-confirm-yes').click();
	await expect(hostMain.getByTestId('binding-notice')).toContainText('revoked');
	const del = lastCall((await apiState(page)).calls, 'removeBinding');
	expect(del.args).toEqual(['gitmcp', SERVER_B]);
	expect(del.payload).toBe(1);
	await expect(hostMain.getByTestId('binding-row')).toHaveCount(1);

	// A stored configuration cannot be deleted while bindings reference it:
	// the producer's 409 is surfaced and the document stays.
	await hostMain.getByTestId('tab-catalog').click();
	const gitRow = hostMain.getByTestId('configured-row').filter({ hasText: 'gitmcp' });
	await gitRow.getByTestId('delete-config').click();
	await expect(hostMain.getByTestId('delete-confirm')).toContainText('gitmcp');
	await gitRow.getByTestId('delete-confirm-yes').click();
	await expect(hostMain.getByTestId('catalog-notice')).toContainText('Grant bindings still exist');
	await expect(gitRow.getByTestId('configured-plugin')).toHaveText('gitmcp');
	await gitRow.getByTestId('delete-confirm-no').click();
	await expect(hostMain.getByTestId('delete-confirm')).toHaveCount(0);

	// Skills: the binding report is declared-minus-granted, never a guess.
	await hostMain.getByTestId('tab-skills').click();
	await expect(hostMain.getByTestId('skill-row')).toHaveCount(1);
	await expect(hostMain.getByTestId('skill-rejection-row')).toContainText('manifest_invalid');
	await expect(hostMain.getByTestId('skill-row').getByTestId('skill-digest')).toContainText('sha256');
	await hostMain.getByTestId('skill-inspect').click();
	await expect(hostMain.getByTestId('skill-package')).toBeVisible();
	await expect(hostMain.getByTestId('skill-package-requires')).toContainText('shell.execute');
	await expect(hostMain.getByTestId('skill-granted-pool')).toHaveText(
		'granted pool: mcp.gitmcp.search_docs'
	);
	await expect(hostMain.getByTestId('skill-missing')).toContainText('shell.execute');
	await expect(hostMain.getByTestId('skill-missing')).toContainText('mcp.gitmcp.fetch_docs');
	await expect(hostMain.getByTestId('skill-instructions')).toContainText('SKILL.md');
	await expect(hostMain.getByTestId('skill-documents').getByTestId('skill-document')).toHaveText(
		'checks.md · sha256 333333333333 · 512 bytes'
	);

	// Hostile skill text stays inert: rendered as text, no node built.
	expect(await page.evaluate(() => window.__maiXssFired ?? false)).toBe(false);
	expect(pageErrors).toEqual([]);
});

test('fail-closed seams, honest errors and stale-revision discipline', async ({ page }) => {
	const hostMain = await mainMounted(page);

	// Without the skills seam the tab renders the honest unavailable note —
	// it never fakes a read; the plugin surfaces stay usable.
	await page.getByTestId('host-noseam').getByTestId('tab-skills').click();
	await expect(page.getByTestId('host-noseam').getByTestId('skills-unavailable')).toBeVisible();
	await expect(page.getByTestId('host-noseam').getByTestId('plugins-root')).toBeVisible();

	// A producer rejection on save is surfaced verbatim; input is kept and no
	// second write is attempted without the operator.
	await hostMain
		.getByTestId('configured-row')
		.filter({ hasText: 'weather' })
		.getByTestId('configure-start')
		.click();
	await expect(hostMain.getByTestId('generated-form')).toBeVisible();
	await apiCall(page, 'failNextEditConfig', [
		{ status: 422, code: 'validation_failed', message: 'config.retries: value does not match declared type' }
	]);
	await hostMain.getByTestId('field-location').fill('Trondheim');
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('config-error')).toHaveText(
		'422 validation_failed: API 422 validation_failed: config.retries: value does not match declared type'
	);
	let { calls } = await apiState(page);
	expect(calls.filter((call) => call.op === 'editConfig').length).toBe(1);
	await expect(hostMain.getByTestId('field-location')).toHaveValue('Trondheim');

	// A stale revision is a displayed conflict with an explicit recovery —
	// never an automatic retry with a fresh revision.
	await apiCall(page, 'setConfigRevision', ['weather', 9]);
	await apiCall(page, 'failNextEditConfig', [
		{ status: 409, code: 'conflict', message: 'stale expected revision or canonical state requires recovery' }
	]);
	await hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('config-conflict')).toContainText('409 conflict');
	calls = (await apiState(page)).calls;
	expect(calls.filter((call) => call.op === 'editConfig').length).toBe(2);

	// The explicit reload re-reads the stored revision triple and discards
	// the stale draft honestly.
	await hostMain.getByTestId('config-conflict-reload').click();
	await expect(hostMain.getByTestId('config-status')).toContainText('Reloaded the stored revision');
	await expect(hostMain.getByTestId('field-location')).toHaveValue('Oslo');
	await expect(hostMain.getByTestId('config-conflict')).toHaveCount(0);

	// A parked write resolves into the panel once released (gate proof: the
	// deferred response is consumed, not dropped).
	await apiCall(page, 'armDeferredSave');
	const savePromise = hostMain.getByTestId('config-save').click();
	await expect(hostMain.getByTestId('config-save')).toBeDisabled();
	await apiCall(page, 'releaseSave', [{ revision: 42, schema_revision: 1, sha256: '9'.repeat(64) }]);
	await savePromise;
	await expect(hostMain.getByTestId('config-status')).toContainText('r42');
	expect(pageErrors).toEqual([]);
});
