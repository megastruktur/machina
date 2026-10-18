import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-panel-registry — proofs for the typed panel registry and the reusable
 * panel host (frontend/src/lib/workspace/registry.ts + PanelHost.svelte).
 *
 * Focused per the test policy: the registry/host pair is exercised in the real
 * Chromium browser against the reviewed 01-web component fixture
 * (tests/fixtures/component-host.svelte) and the integrated frame/state
 * slices — no backend, no invented API, no committed fixture changes.
 *
 * Harness (self-contained; the allowlist covers only this spec, so no config
 * or fixture files are touched): test.beforeAll runs an IN-MEMORY Vite
 * production build (write: false) of virtual modules — a rune-free fixture
 * app (legacy-mode convention recorded in the 01-window-frame evidence) plus
 * FOUR feature-owned fixture panels as separate dynamic-import modules. The
 * build emits real code-split chunks; the route handler serves the entry and
 * each chunk by fileName, so lazy component imports are actual network
 * fetches that the spec observes (positive) and can prove absent (negative:
 * disabled/unknown kinds never fetch their module).
 *
 * Scenario coverage (brief "feature-gate coverage", runtime owner 01-verify):
 *  - actual browser mounts of a labeled fixture panel in a frame AND in
 *    standalone hosts, with explicit scope/services context
 *  - unknown descriptor (ADR §11 "log-viewer" class) handled as a safe error
 *    without breaking the neighbor panel; no dynamic import from data
 *  - intentional render failure contained by the panel-scoped boundary,
 *    generic safe message, retry recovers, neighbor unaffected
 *  - unmount (close and hide paths) releases the subscription fixture exactly
 *    once; remount gets a fresh live context
 *  - registry module: closed typed entry vocabulary (no permission field can
 *    exist), frozen map, per-open scope checks, geometry/instance-key policy
 */

const FIXTURE_URL = '/mai-panel-host-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-panel-host-app.svelte';
const ENTRY_ID = 'virtual:mai-panel-host-entry.js';
const PANEL_ALPHA_ID = 'virtual:mai-panel-host-panel-alpha.svelte';
const PANEL_PREFS_ID = 'virtual:mai-panel-host-panel-prefs.svelte';
const PANEL_BOOM_ID = 'virtual:mai-panel-host-panel-boom.svelte';
const PANEL_OFF_ID = 'virtual:mai-panel-host-panel-off.svelte';

const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const FRAME = `${FRONTEND_ROOT}src/lib/workspace/WindowFrame.svelte`;
const STATE = `${FRONTEND_ROOT}src/lib/workspace/state.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const RUN_ID_A = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';
const DOC_ID_A = 'd0c1a2e3-4b5c-4d6e-8f9a-0b1c2d3e4f50';
const UNKNOWN_WINDOW_ID = '9e1f3c5a-6b2d-4f8e-a7c9-1d3b5f7e9a21';
const DISABLED_WINDOW_ID = '7a2b4c6d-8e0f-4a2b-9c3d-5e6f7a8b9c01';
const DISALLOWED_WINDOW_ID = '3c5e7a9b-1d2f-4a3c-8b4d-6e7f8a9b0c12';
const KEEP_WINDOW_ID = '5e4d3c2b-1a09-4f8e-8b7c-6d5e4f3a2b10';
const CLONE_WINDOW_ID = '2f8e4a6c-0b1d-4e2f-9a3b-5c6d7e8f9a01';

/** Secret-shaped marker: the boom panel's raw error must never reach the DOM. */
const BOOM_SECRET = 'sk-BOOMSECRET';
const BOOM_MARKER = 'boundary probe failure';

/** Unique per-module text markers the spec uses to find each lazy chunk. */
const MARKER_ALPHA = 'alpha-action';
const MARKER_PREFS = 'prefs-action';
const MARKER_BOOM = 'boomAttempts';
const MARKER_OFF = 'panel-off';

/** A raw registry entry WITHOUT `component` (the probe injects a loader fn). */
const VALID_ENTRY = {
	panel_kind: 'server-profile',
	title: 'Server profile',
	scope_types: ['server'],
	scope_object_kinds: ['application', 'document'],
	default_geometry: { x: 40, y: 40, width: 640, height: 400 },
	min_geometry: { x: 0, y: 0, width: 320, height: 200 },
	singleton: false,
	keep_mounted_when_hidden: true,
	enabled: true,
	capabilities_note: 'Read-only profile view.'
};

/**
 * Fixture app — compiles identically under either compiler mode: writable
 * stores + `$store` auto-subscription only (no `export let`, no `$:`).
 * Compiler-mode note (corrects the sibling harness-convention finding): in
 * this in-memory build, vite-plugin-svelte compiles virtual .svelte modules
 * in RUNES mode — `export let` fails with `legacy_export_invalid` (observed
 * 2026-10-13) while `$props()` and `$store` auto-subscription both work. The
 * fixture panels below therefore use `$props()`.
 */
const APP_SOURCE = `
<script>
	import WindowFrame from '${FRAME}';
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createWorkspaceState } from '${STATE}';
	import { createPanelRegistry, singletonInstanceKey } from '${REGISTRY}';
	import { writable } from 'svelte/store';

	const api = window.__maiPanelApi;

	// Transient-failure counter for the fixture-unstable loader (see ENTRIES).
	let unstableAttempts = 0;

	// Explicit services bag — the only services a panel ever sees (no global
	// locator exists anywhere in this fixture).
	const services = Object.freeze({
		clock: Object.freeze({ label: 'fixture-clock 12:00' })
	});

	// Feature-owned entries: loader FUNCTIONS are statically analyzable lazy
	// imports owned by the "feature" (here: fixture modules). 'fixture-off' is
	// registered but disabled — the safe-error path; its module must never be
	// fetched. 'fixture-single' exists for instance-key policy proofs only.
	const ENTRIES = [
		{
			panel_kind: 'fixture-alpha',
			title: 'Alpha',
			scope_types: ['server'],
			scope_object_kinds: ['run', 'conversation'],
			component: () => import('${PANEL_ALPHA_ID}').then((m) => m.default),
			default_geometry: { x: 24, y: 24, width: 420, height: 300 },
			min_geometry: { x: 0, y: 0, width: 280, height: 180 },
			capabilities_note: 'Fixture only; registration grants no capability.'
		},
		{
			panel_kind: 'fixture-global',
			title: 'Prefs',
			scope_types: ['global'],
			component: () => import('${PANEL_PREFS_ID}').then((m) => m.default),
			default_geometry: { x: 470, y: 24, width: 360, height: 240 },
			singleton: true,
			keep_mounted_when_hidden: true
		},
		{
			panel_kind: 'fixture-boom',
			title: 'Boom',
			scope_types: ['global'],
			component: () => import('${PANEL_BOOM_ID}').then((m) => m.default)
		},
		{
			panel_kind: 'fixture-off',
			title: 'Off',
			scope_types: ['server'],
			component: () => import('${PANEL_OFF_ID}').then((m) => m.default),
			enabled: false
		},
		{
			panel_kind: 'fixture-single',
			title: 'Single',
			scope_types: ['server'],
			component: () => import('${PANEL_PREFS_ID}').then((m) => m.default),
			singleton: true
		},
		{
			panel_kind: 'fixture-unstable',
			title: 'Unstable',
			scope_types: ['global'],
			// Load-retry proof: the loader rejects exactly once, then resolves.
			// (Chromium caches failed module URLs for the page's lifetime, so a
			// static import alone could never succeed on retry.)
			component: function () {
				unstableAttempts += 1;
				api.unstableAttempts = unstableAttempts;
				if (unstableAttempts === 1) {
					return Promise.reject(new Error('transient fixture load failure'));
				}
				return import('${PANEL_PREFS_ID}').then((m) => m.default);
			}
		}
	];

	const registryResult = createPanelRegistry(ENTRIES);
	if (!registryResult.ok) {
		throw new Error('fixture registry must validate: ' + registryResult.issue.code);
	}
	const registry = registryResult.value;

	const state = createWorkspaceState({
		knownPanelKinds: registry.knownPanelKinds(),
		instanceKey: function (kind, scope) {
			const entry = registry.entry(kind);
			if (!entry) return null;
			return singletonInstanceKey(kind, entry, scope);
		}
	});

	api.registryKinds = Array.from(registry.knownPanelKinds()).sort();

	// --- composed open path: registry scope check BEFORE the state open -----
	function openPanel(kind, scope, geometry) {
		const check = registry.checkScope(kind, scope);
		if (!check.ok) {
			return { stage: 'scope', ok: false, code: check.issue.code };
		}
		const outcome = state.open({ panel_kind: kind, scope, geometry });
		return outcome.ok
			? { stage: 'state', ok: true, windowId: outcome.descriptor.window_id, created: outcome.created }
			: { stage: 'state', ok: false, code: outcome.issue.code };
	}

	api.openPanel = openPanel;
	api.openRaw = function (kind, scope, geometry) {
		const outcome = state.open({ panel_kind: kind, scope, geometry });
		return outcome.ok
			? { ok: true, windowId: outcome.descriptor.window_id }
			: { ok: false, code: outcome.issue.code };
	};
	api.checkScope = function (kind, scope) {
		const result = registry.checkScope(kind, scope);
		return result.ok
			? { ok: true }
			: { ok: false, code: result.issue.code, message: result.issue.message };
	};
	api.geometry = {
		defaultOf: function (kind) { return registry.defaultGeometry(kind); },
		minOf: function (kind) { return registry.minGeometry(kind); }
	};
	api.instanceKey = function (kind, scope) {
		const entry = registry.entry(kind);
		if (!entry) return null;
		return singletonInstanceKey(kind, entry, scope);
	};
	api.snapshot = function () { return state.snapshot(); };

	// --- fixture windows -----------------------------------------------------
	const alphaOpen = openPanel('fixture-alpha', {
		kind: 'server',
		server_id: '${SERVER_A}',
		object: { kind: 'run', id: '${RUN_ID_A}' }
	}, registry.defaultGeometry('fixture-alpha'));
	const globalOpen = openPanel('fixture-global', { kind: 'global' }, registry.defaultGeometry('fixture-global'));
	const boomOpen = openPanel('fixture-boom', { kind: 'global' }, { x: 860, y: 24, width: 220, height: 180 });
	const unstableOpen = openPanel('fixture-unstable', { kind: 'global' }, { x: 24, y: 344, width: 300, height: 120 });
	if (!alphaOpen.ok || !globalOpen.ok || !boomOpen.ok || !unstableOpen.ok) {
		throw new Error('fixture opens must validate');
	}
	api.windowIds = {
		alpha: alphaOpen.windowId,
		global: globalOpen.windowId,
		boom: boomOpen.windowId,
		unstable: unstableOpen.windowId
	};

	// Standalone-host descriptors (host is reusable without a frame). The
	// unknown kind is the ADR §11 "log-viewer" stale-layout class: the host
	// itself must render the safe error when handed such a descriptor.
	const unknownDescriptor = {
		window_id: '${UNKNOWN_WINDOW_ID}',
		panel_kind: 'log-viewer',
		scope: { kind: 'server', server_id: '${SERVER_B}' },
		geometry: { x: 0, y: 0, width: 300, height: 200 },
		presentation: 'normal',
		z: 1,
		title_override: null
	};
	const disabledDescriptor = {
		window_id: '${DISABLED_WINDOW_ID}',
		panel_kind: 'fixture-off',
		scope: { kind: 'server', server_id: '${SERVER_A}' },
		geometry: { x: 0, y: 0, width: 300, height: 200 },
		presentation: 'normal',
		z: 1,
		title_override: null
	};
	const disallowedDescriptor = {
		window_id: '${DISALLOWED_WINDOW_ID}',
		panel_kind: 'fixture-alpha',
		scope: { kind: 'server', server_id: '${SERVER_A}', object: { kind: 'document', id: '${DOC_ID_A}' } },
		geometry: { x: 0, y: 0, width: 300, height: 200 },
		presentation: 'normal',
		z: 1,
		title_override: null
	};

	// Standalone alpha host: a descriptor COPY of the alpha window pinned to a
	// second server with no object (distinct view identity; not a state
	// window). The base is captured ONCE at setup — the copy must stay stable
	// even after the state window it was cloned from is closed in a test.
	const cloneBase = (function () {
		const found = state.snapshot().windows.find(function (w) {
			return w.window_id === alphaOpen.windowId;
		});
		const next = JSON.parse(JSON.stringify(found));
		next.window_id = '${CLONE_WINDOW_ID}';
		next.scope = { kind: 'server', server_id: '${SERVER_B}' };
		return next;
	})();
	function cloneDescriptor(presentation) {
		const next = JSON.parse(JSON.stringify(cloneBase));
		next.presentation = presentation;
		return next;
	}
	const cloneStore = writable(cloneDescriptor('normal'));
	api.setClonePresentation = function (presentation) {
		cloneStore.set(cloneDescriptor(presentation));
	};

	// keep_mounted_when_hidden=true counterpart: a prefs descriptor copy whose
	// presentation the fixture controls; the host must KEEP the content mounted.
	const keepBase = (function () {
		const found = state.snapshot().windows.find(function (w) {
			return w.window_id === globalOpen.windowId;
		});
		const next = JSON.parse(JSON.stringify(found));
		next.window_id = '${KEEP_WINDOW_ID}';
		return next;
	})();
	function keepDescriptor(presentation) {
		const next = JSON.parse(JSON.stringify(keepBase));
		next.presentation = presentation;
		return next;
	}
	const keepStore = writable(keepDescriptor('normal'));
	api.setKeepPresentation = function (presentation) {
		keepStore.set(keepDescriptor(presentation));
	};

	api.closeIntents = [];

	function recordError(windowId, panelKind, error) {
		api.errors.push({
			windowId,
			panelKind,
			message: error && error.message ? String(error.message) : String(error)
		});
	}

	const titlesByKind = {
		'fixture-alpha': 'Alpha',
		'fixture-global': 'Prefs',
		'fixture-boom': 'Boom'
	};

	const snapshotStore = writable(state.snapshot());
	state.subscribe(function (next) { snapshotStore.set(next); });
</script>

<ComponentHost label="MAI panel host fixture">
	<div class="mai-desktop" data-testid="desktop" style="position: relative; width: 1100px; height: 480px;">
		{#each $snapshotStore.windows as descriptor (descriptor.window_id)}
			<WindowFrame
				{descriptor}
				title={titlesByKind[descriptor.panel_kind] ?? descriptor.panel_kind}
				contextLabel="Server Alpha"
				focused={$snapshotStore.focused_id === descriptor.window_id}
				onactivate={(id) => state.focus(id)}
				onclose={(id) => {
					api.closeIntents.push(id);
					state.close(id);
				}}
			>
				<div class="mai-frame-body">
					<PanelHost registry={registry} {descriptor} {services} onpanelerror={recordError} />
				</div>
			</WindowFrame>
		{/each}
	</div>
	<div class="mai-strip" data-testid="standalone-strip">
		<PanelHost registry={registry} descriptor={unknownDescriptor} {services} onpanelerror={recordError} class="mai-standalone" />
		<PanelHost registry={registry} descriptor={disabledDescriptor} {services} onpanelerror={recordError} class="mai-standalone" />
		<PanelHost registry={registry} descriptor={disallowedDescriptor} {services} onpanelerror={recordError} class="mai-standalone" />
		<PanelHost registry={registry} descriptor={$cloneStore} {services} onpanelerror={recordError} class="mai-standalone" />
		<PanelHost registry={registry} descriptor={$keepStore} {services} onpanelerror={recordError} class="mai-standalone" />
	</div>
</ComponentHost>

<style>
	.mai-frame-body {
		height: 100%;
	}
	.mai-strip {
		display: flex;
		gap: var(--mai-space-4);
		padding: var(--mai-space-4);
	}
	.mai-standalone {
		width: 320px;
		height: 180px;
		border: var(--mai-border-width) solid var(--mai-color-border);
	}
</style>
`;

/** Fixture panels — feature-owned modules, each its own lazy chunk. */

const PANEL_ALPHA_SOURCE = `
<script>
	let { panel } = $props();

	const api = window.__maiPanelApi;
	api.mounts.push({ panel: 'alpha', windowId: panel.descriptor.window_id });

	// Mount-scoped subscription fixture: the host must run this exactly once
	// per content unmount (hide or close), never zero, never twice.
	panel.onCleanup(function () {
		api.releases.push(panel.descriptor.window_id);
	});

	const scope = panel.scope;
	const scopeText = scope.kind + '|' + scope.server_id + '|' +
		(scope.object ? scope.object.kind + '|' + scope.object.id : 'none');
	const serviceText = String(panel.services.clock.label);
</script>

<div data-testid="panel-alpha">
	<p data-testid="panel-alpha-scope">{scopeText}</p>
	<p data-testid="panel-alpha-service">{serviceText}</p>
	<button type="button" data-click-count="${MARKER_ALPHA}">Alpha action</button>
</div>
`;

const PANEL_PREFS_SOURCE = `
<script>
	let { panel } = $props();

	const api = window.__maiPanelApi;
	api.mounts.push({ panel: 'prefs', windowId: panel.descriptor.window_id });

	const scopeKind = panel.scope.kind;
</script>

<div data-testid="panel-prefs">
	<p data-testid="panel-prefs-scope">{scopeKind}</p>
	<button type="button" data-click-count="${MARKER_PREFS}">Prefs action</button>
</div>
`;

const PANEL_BOOM_SOURCE = `
<script>
	let { panel } = $props();

	const api = window.__maiPanelApi;
	api.boomAttempts += 1;
	if (api.boomAttempts === 1) {
		// Intentional first-mount failure with secret-shaped content: the
		// boundary must keep this text out of the DOM entirely.
		throw new Error('token=${BOOM_SECRET} ${BOOM_MARKER}');
	}
	api.mounts.push({ panel: 'boom', windowId: panel.descriptor.window_id });
	const attempt = api.boomAttempts;
</script>

<div data-testid="panel-boom">
	<p>Boom recovered on attempt {attempt}</p>
</div>
`;

const PANEL_OFF_SOURCE = `
<script>
	let { panel } = $props();
	window.__maiPanelApi.mounts.push({ panel: 'off', windowId: panel.descriptor.window_id });
</script>

<div data-testid="${MARKER_OFF}"><p>off</p></div>
`;

const ENTRY_SOURCE = `
import { mount } from 'svelte';
import * as RegistryModule from '${REGISTRY}';
import App from '${APP_ID}';
import '${TOKENS}';

const api = {
	mounts: [],
	releases: [],
	errors: [],
	closeIntents: [],
	clicks: {},
	boomAttempts: 0,
	unstableAttempts: 0,
	windowIds: null,
	registryKinds: null,
	openPanel: null,
	openRaw: null,
	checkScope: null,
	geometry: null,
	instanceKey: null,
	defineEntry: null,
	createRegistry: null,
	snapshot: null,
	setClonePresentation: null,
	setKeepPresentation: null
};
window.__maiPanelApi = api;

// Inert fixture content stays event-free; interaction counts use delegation
// so the tests read native click outcomes only.
document.addEventListener('click', function (event) {
	if (!(event.target instanceof Element)) return;
	const sample = event.target.closest('[data-click-count]');
	if (sample) {
		const key = sample.getAttribute('data-click-count') ?? '';
		api.clicks[key] = (api.clicks[key] ?? 0) + 1;
	}
});

// Module-level probes: a fixed in-page loader is injected because functions
// cannot cross the CDP boundary; an explicit component field overrides it.
api.defineEntry = function (raw) {
	const candidate = Object.assign({ component: function () { return Promise.resolve({}); } }, raw);
	const result = RegistryModule.definePanelEntry(candidate);
	if (!result.ok) {
		return { ok: false, code: result.issue.code, message: result.issue.message };
	}
	const value = result.value;
	return {
		ok: true,
		value: {
			panel_kind: value.panel_kind,
			title: value.title,
			scope_types: Array.from(value.scope_types),
			scope_object_kinds: Array.from(value.scope_object_kinds),
			componentIsFunction: typeof value.component === 'function',
			default_geometry: value.default_geometry,
			min_geometry: value.min_geometry,
			singleton: value.singleton,
			keep_mounted_when_hidden: value.keep_mounted_when_hidden,
			enabled: value.enabled,
			capabilities_note: value.capabilities_note
		}
	};
};

api.createRegistry = function (rawEntries) {
	const entries = rawEntries.map(function (raw) {
		return Object.assign({ component: function () { return Promise.resolve({}); } }, raw);
	});
	const result = RegistryModule.createPanelRegistry(entries);
	if (!result.ok) {
		return { ok: false, code: result.issue.code };
	}
	return {
		ok: true,
		kinds: Array.from(result.value.knownPanelKinds()).sort(),
		unknownEntryIsNull: result.value.entry('log-viewer') === null
	};
};

mount(App, { target: document.getElementById('app'), props: { api } });
`;

/** Minimal bundle shape (same structural boundary as the sibling specs). */
type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; fileName: string; code: string; isEntry: boolean }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

type ServedChunk = { fileName: string; code: string };

/** Probe results crossing the CDP boundary (plain JSON only). */
interface ProbeMount {
	panel: string;
	windowId: string;
}
interface ProbeError {
	windowId: string;
	panelKind: string;
	message: string;
}
interface ProbeWindowIds {
	alpha: string;
	global: string;
	boom: string;
	unstable: string;
}
type ScopeCheck = { ok: true } | { ok: false; code: string; message: string };
type EntryResult =
	| { ok: true; value: Record<string, unknown> }
	| { ok: false; code: string; message: string };
type RegistryResult = { ok: true; kinds: string[]; unknownEntryIsNull: boolean } | { ok: false; code: string };
type OpenResult =
	| { stage: 'scope'; ok: false; code: string }
	| { stage: 'state'; ok: true; windowId: string; created: boolean }
	| { stage: 'state'; ok: false; code: string };

interface ProbeApi {
	mounts: ProbeMount[];
	releases: string[];
	errors: ProbeError[];
	closeIntents: string[];
	clicks: Record<string, number>;
	boomAttempts: number;
	unstableAttempts: number;
	windowIds: ProbeWindowIds | null;
	registryKinds: string[] | null;
	openPanel(kind: string, scope: unknown, geometry: unknown): OpenResult;
	openRaw(kind: string, scope: unknown, geometry: unknown): { ok: true; windowId: string } | { ok: false; code: string };
	checkScope(kind: string, scope: unknown): ScopeCheck;
	geometry: { defaultOf(kind: string): unknown; minOf(kind: string): unknown };
	instanceKey(kind: string, scope: unknown): string | null;
	defineEntry(raw: Record<string, unknown>): EntryResult;
	createRegistry(rawEntries: Array<Record<string, unknown>>): RegistryResult;
	snapshot(): { windows: Array<Record<string, unknown>>; focused_id: string | null };
	setClonePresentation(presentation: string): void;
	setKeepPresentation(presentation: string): void;
}

declare global {
	interface Window {
		/** Spec-unique probe global (no collision with the sibling specs). */
		__maiPanelApi?: ProbeApi;
	}
}

let harnessJs = '';
let harnessCss = '';
let chunks: ServedChunk[] = [];

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(): Plugin {
	const virtualSources: Record<string, string> = {
		[APP_ID]: APP_SOURCE,
		[PANEL_ALPHA_ID]: PANEL_ALPHA_SOURCE,
		[PANEL_PREFS_ID]: PANEL_PREFS_SOURCE,
		[PANEL_BOOM_ID]: PANEL_BOOM_SOURCE,
		[PANEL_OFF_ID]: PANEL_OFF_SOURCE
	};
	return {
		name: 'mai-panel-host-harness',
		resolveId(id) {
			// .svelte virtual modules must NOT get the conventional `\0` prefix:
			// vite-plugin-svelte excludes `\0` ids from Svelte compilation
			// (01-window-frame evidence). The JS entry keeps the `\0`.
			// Virtual .svelte modules compile in RUNES mode in this build — the
			// fixture panels use `$props()` (see the harness note above).
			if (id in virtualSources) return id;
			if (id === ENTRY_ID) return `\0${id}`;
			return undefined;
		},
		load(id) {
			if (id in virtualSources) return virtualSources[id];
			if (id === `\0${ENTRY_ID}`) return ENTRY_SOURCE;
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin()],
		build: {
			write: false,
			emptyOutDir: true,
			cssCodeSplit: false,
			outDir: 'node_modules/.mai-panel-host-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'panelhost-[hash].js',
					assetFileNames: 'demo[extname]'
				}
			}
		}
	});

	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	chunks = [];
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
	for (const marker of [MARKER_ALPHA, MARKER_PREFS, MARKER_BOOM, MARKER_OFF]) {
		if (!chunkFileName(marker)) {
			throw new Error(`harness bundle incomplete: no lazy chunk found for marker ${marker}`);
		}
	}
});

/** The lazy chunk carrying `text`, or null (asserted non-null at build time). */
function chunkFileName(text: string): string | null {
	const found = chunks.find((chunk) => chunk.code.includes(text));
	return found ? found.fileName : null;
}

let servedChunks: string[] = [];

test.beforeEach(async ({ page }) => {
	servedChunks = [];
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI panel host fixture</title>
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

/** Navigates and waits until the fixture app has exposed the probe API. */
async function gotoProbe(page: Page): Promise<ProbeWindowIds> {
	await page.goto(FIXTURE_URL);
	await page.waitForFunction(() => window.__maiPanelApi?.windowIds !== null);
	const ids = await page.evaluate(() => window.__maiPanelApi!.windowIds);
	if (!ids) throw new Error('probe window ids missing');
	return ids;
}

/** In-page probe accessors (functions themselves never cross the CDP boundary). */
const probe = {
	mounts: (page: Page) => page.evaluate(() => window.__maiPanelApi!.mounts),
	releases: (page: Page) => page.evaluate(() => window.__maiPanelApi!.releases),
	errors: (page: Page) => page.evaluate(() => window.__maiPanelApi!.errors),
	clicks: (page: Page) => page.evaluate(() => window.__maiPanelApi!.clicks),
	closeIntents: (page: Page) => page.evaluate(() => window.__maiPanelApi!.closeIntents),
	boomAttempts: (page: Page) => page.evaluate(() => window.__maiPanelApi!.boomAttempts),
	unstableAttempts: (page: Page) => page.evaluate(() => window.__maiPanelApi!.unstableAttempts),
	snapshot: (page: Page) => page.evaluate(() => window.__maiPanelApi!.snapshot()),
	openPanel: (page: Page, kind: string, scope: unknown, geometry: unknown) =>
		page.evaluate(
			([k, s, g]) => window.__maiPanelApi!.openPanel(k, s, g),
			[kind, scope, geometry] as const
		),
	openRaw: (page: Page, kind: string, scope: unknown, geometry: unknown) =>
		page.evaluate(
			([k, s, g]) => window.__maiPanelApi!.openRaw(k, s, g),
			[kind, scope, geometry] as const
		),
	checkScope: (page: Page, kind: string, scope: unknown) =>
		page.evaluate(
			([k, s]) => window.__maiPanelApi!.checkScope(k, s),
			[kind, scope] as const
		),
	defaultGeometry: (page: Page, kind: string) =>
		page.evaluate((kind) => window.__maiPanelApi!.geometry.defaultOf(kind), kind),
	minGeometry: (page: Page, kind: string) =>
		page.evaluate((kind) => window.__maiPanelApi!.geometry.minOf(kind), kind),
	instanceKey: (page: Page, kind: string, scope: unknown) =>
		page.evaluate(
			([k, s]) => window.__maiPanelApi!.instanceKey(k, s),
			[kind, scope] as const
		),
	defineEntry: (page: Page, raw: Record<string, unknown>) =>
		page.evaluate((raw) => window.__maiPanelApi!.defineEntry(raw), raw),
	createRegistry: (page: Page, rawEntries: Array<Record<string, unknown>>) =>
		page.evaluate((rawEntries) => window.__maiPanelApi!.createRegistry(rawEntries), rawEntries),
	setClonePresentation: (page: Page, presentation: string) =>
		page.evaluate((presentation) => window.__maiPanelApi!.setClonePresentation(presentation), presentation),
	setKeepPresentation: (page: Page, presentation: string) =>
		page.evaluate((presentation) => window.__maiPanelApi!.setKeepPresentation(presentation), presentation)
};

const globalScope = { kind: 'global' };

/** Mount-cycle count for one panel/window pair (mount ORDER is scheduler-dependent). */
function mountCount(mounts: ProbeMount[], panel: string, windowId: string): number {
	return mounts.filter((mount) => mount.panel === panel && mount.windowId === windowId).length;
}

const serverScopeA = { kind: 'server', server_id: SERVER_A };
const serverScopeB = { kind: 'server', server_id: SERVER_B };
const serverRunScopeA = { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } };
const serverDocScopeA = { kind: 'server', server_id: SERVER_A, object: { kind: 'document', id: DOC_ID_A } };
const OPEN_GEOMETRY = { x: 8, y: 8, width: 400, height: 300 };

// ---------------------------------------------------------------------------
// Module-level registry proofs
// ---------------------------------------------------------------------------

test('registry module mounts with the fixture and exports the frozen kind set', async ({ page }) => {
	const ids = await gotoProbe(page);
	expect(typeof ids.alpha).toBe('string');
	const kinds = await page.evaluate(() => window.__maiPanelApi!.registryKinds);
	expect(kinds).toEqual([
		'fixture-alpha',
		'fixture-boom',
		'fixture-global',
		'fixture-off',
		'fixture-single',
		'fixture-unstable'
	]);
});

test('definePanelEntry normalizes the closed frozen vocabulary', async ({ page }) => {
	await gotoProbe(page);
	const result = await probe.defineEntry(page, VALID_ENTRY);
	expect(result.ok).toBe(true);
	if (!result.ok) return;
	expect(result.value).toEqual({
		panel_kind: 'server-profile',
		title: 'Server profile',
		scope_types: ['server'],
		scope_object_kinds: ['application', 'document'],
		componentIsFunction: true,
		default_geometry: { x: 40, y: 40, width: 640, height: 400 },
		min_geometry: { x: 0, y: 0, width: 320, height: 200 },
		singleton: false,
		keep_mounted_when_hidden: true,
		enabled: true,
		capabilities_note: 'Read-only profile view.'
	});
});

test('the component slot accepts loader functions only — URLs and paths are rejected', async ({
	page
}) => {
	await gotoProbe(page);
	const url = await probe.defineEntry(page, {
		...VALID_ENTRY,
		component: 'https://evil.test/panel.js'
	});
	expect(url).toMatchObject({ ok: false, code: 'bad_component' });
	const path = await probe.defineEntry(page, { ...VALID_ENTRY, component: '../panels/LogPanel.svelte' });
	expect(path).toMatchObject({ ok: false, code: 'bad_component' });
	const number = await probe.defineEntry(page, { ...VALID_ENTRY, component: 42 });
	expect(number).toMatchObject({ ok: false, code: 'bad_component' });
});

test('registration grants no permissions: the entry vocabulary has no permission field', async ({
	page
}) => {
	await gotoProbe(page);
	const grants = await probe.defineEntry(page, { ...VALID_ENTRY, grants: ['shell:root'] });
	expect(grants).toMatchObject({ ok: false, code: 'unknown_field' });
	const permissions = await probe.defineEntry(page, { ...VALID_ENTRY, permissions: '*' });
	expect(permissions).toMatchObject({ ok: false, code: 'unknown_field' });
});

test('entry validation rejects malformed vocabulary fields', async ({ page }) => {
	await gotoProbe(page);
	for (const panel_kind of ['Chat', '', 'chat--x', 'chat x', '1chat']) {
		expect(await probe.defineEntry(page, { ...VALID_ENTRY, panel_kind })).toMatchObject({
			ok: false,
			code: 'bad_panel_kind'
		});
	}
	expect(await probe.defineEntry(page, { ...VALID_ENTRY, title: 'token=sk-abc123' })).toMatchObject({
		ok: false,
		code: 'bad_title'
	});
	expect(await probe.defineEntry(page, { ...VALID_ENTRY, title: '   ' })).toMatchObject({
		ok: false,
		code: 'bad_title'
	});
	expect(
		await probe.defineEntry(page, { ...VALID_ENTRY, scope_types: [], scope_object_kinds: [] })
	).toMatchObject({ ok: false, code: 'bad_scope_types' });
	expect(await probe.defineEntry(page, { ...VALID_ENTRY, scope_types: ['modal'] })).toMatchObject({
		ok: false,
		code: 'bad_scope_types'
	});
	expect(
		await probe.defineEntry(page, { ...VALID_ENTRY, scope_types: ['global'], scope_object_kinds: ['run'] })
	).toMatchObject({ ok: false, code: 'bad_scope_object_kinds' });
	expect(
		await probe.defineEntry(page, { ...VALID_ENTRY, scope_object_kinds: ['widget'] })
	).toMatchObject({ ok: false, code: 'bad_scope_object_kinds' });
	expect(
		await probe.defineEntry(page, { ...VALID_ENTRY, default_geometry: { x: 0, y: 0, width: NaN, height: 200 } })
	).toMatchObject({ ok: false, code: 'bad_geometry' });
	expect(
		await probe.defineEntry(page, {
			...VALID_ENTRY,
			default_geometry: { x: 0, y: 0, width: 100, height: 100 },
			min_geometry: { x: 0, y: 0, width: 320, height: 200 }
		})
	).toMatchObject({ ok: false, code: 'bad_geometry' });
	expect(await probe.defineEntry(page, { ...VALID_ENTRY, singleton: 'yes' })).toMatchObject({
		ok: false,
		code: 'bad_singleton'
	});
	expect(await probe.defineEntry(page, { ...VALID_ENTRY, keep_mounted_when_hidden: 1 })).toMatchObject({
		ok: false,
		code: 'bad_keep_mounted_when_hidden'
	});
	expect(await probe.defineEntry(page, { ...VALID_ENTRY, enabled: 'true' })).toMatchObject({
		ok: false,
		code: 'bad_enabled'
	});
	expect(
		await probe.defineEntry(page, { ...VALID_ENTRY, capabilities_note: 'password=hunter2' })
	).toMatchObject({ ok: false, code: 'bad_capabilities_note' });
});

test('createPanelRegistry rejects empty and duplicate sets and freezes the kind map', async ({ page }) => {
	await gotoProbe(page);
	expect(await probe.createRegistry(page, [])).toEqual({ ok: false, code: 'empty_entries' });
	const duplicated = await probe.createRegistry(page, [
		{ panel_kind: 'dup', title: 'A', scope_types: ['global'] },
		{ panel_kind: 'dup', title: 'B', scope_types: ['server'] }
	]);
	expect(duplicated).toEqual({ ok: false, code: 'duplicate_panel_kind' });
	const ok = await probe.createRegistry(page, [
		{ panel_kind: 'k-one', title: 'One', scope_types: ['global'] },
		{ panel_kind: 'k-two', title: 'Two', scope_types: ['server'] }
	]);
	expect(ok).toEqual({
		ok: true,
		kinds: ['k-one', 'k-two'],
		unknownEntryIsNull: true
	});
});

test('checkScope enforces declared scope types and pinned object kinds per entry', async ({ page }) => {
	await gotoProbe(page);
	expect(await probe.checkScope(page, 'fixture-alpha', serverRunScopeA)).toEqual({ ok: true });
	expect(await probe.checkScope(page, 'fixture-alpha', serverScopeB)).toEqual({ ok: true });
	expect(await probe.checkScope(page, 'fixture-alpha', globalScope)).toMatchObject({
		ok: false,
		code: 'scope_kind_not_allowed'
	});
	expect(await probe.checkScope(page, 'fixture-global', serverScopeA)).toMatchObject({
		ok: false,
		code: 'scope_kind_not_allowed'
	});
	expect(await probe.checkScope(page, 'fixture-alpha', serverDocScopeA)).toMatchObject({
		ok: false,
		code: 'scope_object_kind_not_allowed'
	});
	expect(await probe.checkScope(page, 'fixture-off', serverScopeA)).toMatchObject({
		ok: false,
		code: 'panel_disabled'
	});
	expect(await probe.checkScope(page, 'log-viewer', serverScopeA)).toMatchObject({
		ok: false,
		code: 'unknown_panel_kind'
	});
});

test('geometry policy: entry defaults resolve, omitted minimums fall back to the frozen global', async ({
	page
}) => {
	await gotoProbe(page);
	expect(await probe.defaultGeometry(page, 'fixture-alpha')).toEqual({ x: 24, y: 24, width: 420, height: 300 });
	expect(await probe.defaultGeometry(page, 'fixture-boom')).toEqual(null);
	expect(await probe.minGeometry(page, 'fixture-alpha')).toEqual({ x: 0, y: 0, width: 280, height: 180 });
	expect(await probe.minGeometry(page, 'fixture-boom')).toEqual({ width: 320, height: 200 });
});

test('singleton instance keys deduplicate per pinned scope and never for free kinds', async ({ page }) => {
	await gotoProbe(page);
	expect(await probe.instanceKey(page, 'fixture-alpha', serverScopeA)).toBe(null);
	expect(await probe.instanceKey(page, 'fixture-global', globalScope)).toBe('fixture-global::global');
	expect(await probe.instanceKey(page, 'fixture-single', serverRunScopeA)).toBe(
		`fixture-single::server/${SERVER_A}/run/${RUN_ID_A}`
	);
	const otherRun = await probe.instanceKey(page, 'fixture-single', {
		kind: 'server',
		server_id: SERVER_A,
		object: { kind: 'run', id: DOC_ID_A }
	});
	expect(otherRun).not.toBe(await probe.instanceKey(page, 'fixture-single', serverRunScopeA));
	expect(await probe.instanceKey(page, 'fixture-single', serverScopeB)).toBe(
		`fixture-single::server/${SERVER_B}`
	);
});

// ---------------------------------------------------------------------------
// Real-browser host lifecycle proofs
// ---------------------------------------------------------------------------

test('labeled fixture panels mount in a frame and standalone through real lazy imports', async ({
	page
}) => {
	const ids = await gotoProbe(page);

	// Frame-mounted alpha: full context (scope kind, server, pinned run object).
	const alphaFrame = page.locator(`[role="dialog"][data-panel-kind="fixture-alpha"][data-window-id="${ids.alpha}"]`);
	await expect(alphaFrame).toBeVisible();
	await expect(alphaFrame.locator('[data-testid="panel-alpha-scope"]')).toHaveText(
		`server|${SERVER_A}|run|${RUN_ID_A}`
	);
	await expect(alphaFrame.locator('[data-testid="panel-alpha-service"]')).toHaveText('fixture-clock 12:00');
	await expect(alphaFrame).toContainText('Alpha — Server Alpha');

	// Standalone hosts: the global-scope panel appears both framed (the prefs
	// window) and standalone (the keep-mounted copy); the alpha copy pins a
	// second server with no object — non-singleton kinds open freely and the
	// two instances never share scope.
	const prefsFrame = page.locator(`[role="dialog"][data-window-id="${ids.global}"]`);
	await expect(prefsFrame.locator('[data-testid="panel-prefs-scope"]')).toHaveText('global');
	const keepHost = page.locator(`.mai-panel-host[data-window-id="${KEEP_WINDOW_ID}"]`);
	await expect(keepHost.locator('[data-testid="panel-prefs-scope"]')).toHaveText('global');
	const cloneHost = page.locator(`.mai-panel-host[data-window-id="${CLONE_WINDOW_ID}"]`);
	await expect(cloneHost.locator('[data-testid="panel-alpha-scope"]')).toHaveText(
		`server|${SERVER_B}|none`
	);

	// Lazy resolution was a real network fetch of each feature chunk.
	expect(servedChunks).toContain(chunkFileName(MARKER_ALPHA));
	expect(servedChunks).toContain(chunkFileName(MARKER_PREFS));

	// Registry default_geometry resolved the prefs frame geometry end-to-end.
	await expect(prefsFrame).toHaveCSS('width', '360px');
	await expect(prefsFrame).toHaveCSS('height', '240px');

	// Panel content is genuinely interactive in both mount shapes.
	await alphaFrame.locator(`[data-click-count="${MARKER_ALPHA}"]`).click();
	await keepHost.locator(`[data-click-count="${MARKER_PREFS}"]`).click();
	expect(await probe.clicks(page)).toEqual({
		[MARKER_ALPHA]: 1,
		[MARKER_PREFS]: 1
	});
});

test('unknown panel kind renders the safe error view without any import attempt', async ({ page }) => {
	await gotoProbe(page);

	const unknownHost = page.locator(`.mai-panel-host[data-window-id="${UNKNOWN_WINDOW_ID}"]`);
	const unavailable = unknownHost.locator('[data-testid="panel-unavailable"]');
	await expect(unavailable).toBeVisible();
	await expect(unavailable).toHaveAttribute('data-reason', 'unknown_panel_kind');
	await expect(unavailable).toHaveText('This panel is not available.');
	await expect(unknownHost.locator('[data-testid="panel-body"]')).toHaveCount(0);

	// The boom fixture's render failure (same initial mount pass) proves the
	// whole window set finished mounting before the chunk-set assertions.
	const ids = await page.evaluate(() => window.__maiPanelApi!.windowIds);
	await expect(
		page.locator(`[role="dialog"][data-window-id="${ids!.boom}"] [data-testid="panel-render-failed"]`)
	).toBeVisible();

	// No dynamic import from data: the disabled entry's module was never
	// fetched, and the served set is exactly the reachable fixture chunks.
	expect(servedChunks).not.toContain(chunkFileName(MARKER_OFF));
	expect([...servedChunks].sort()).toEqual(
		[chunkFileName(MARKER_ALPHA), chunkFileName(MARKER_PREFS), chunkFileName(MARKER_BOOM)].sort()
	);

	// The neighbor panel is untouched and still interactive.
	const alphaFrame = page.locator(`[role="dialog"][data-window-id="${ids!.alpha}"]`);
	await alphaFrame.locator(`[data-click-count="${MARKER_ALPHA}"]`).click();
	expect(await probe.clicks(page)).toEqual({ [MARKER_ALPHA]: 1 });
});

test('registered-but-disabled kind is the same safe error and its module is never fetched', async ({
	page
}) => {
	await gotoProbe(page);
	const disabledHost = page.locator(`.mai-panel-host[data-window-id="${DISABLED_WINDOW_ID}"]`);
	const unavailable = disabledHost.locator('[data-testid="panel-unavailable"]');
	await expect(unavailable).toBeVisible();
	await expect(unavailable).toHaveAttribute('data-reason', 'panel_disabled');
	await expect(disabledHost.locator('[data-testid="panel-body"]')).toHaveCount(0);
	expect(servedChunks).not.toContain(chunkFileName(MARKER_OFF));
});

test('a descriptor with a disallowed pinned scope renders the safe error, never content', async ({
	page
}) => {
	await gotoProbe(page);
	const disallowedHost = page.locator(`.mai-panel-host[data-window-id="${DISALLOWED_WINDOW_ID}"]`);
	await expect(disallowedHost.locator('[data-testid="panel-unavailable"]')).toBeVisible();
	await expect(disallowedHost.locator('[data-testid="panel-unavailable"]')).toHaveAttribute(
		'data-reason',
		'scope_object_kind_not_allowed'
	);
	await expect(disallowedHost.locator('[data-testid="panel-alpha"]')).toHaveCount(0);
});

test('intentional render failure is contained: generic message, retry recovers, neighbors unaffected', async ({
	page
}) => {
	const ids = await gotoProbe(page);
	const boomHost = page.locator(`.mai-panel-host[data-window-id="${ids.boom}"]`);

	// First mount throws with secret-shaped content.
	const failed = boomHost.locator('[data-testid="panel-render-failed"]');
	await expect(failed).toBeVisible();
	await expect(failed.locator('p')).toHaveText('This panel could not be rendered.');
	await expect(failed.getByTestId('panel-render-retry')).toBeVisible();

	// The raw error text never entered the DOM — only the generic message.
	const body = await page.locator('body').innerText();
	expect(body).not.toContain(BOOM_SECRET);
	expect(body).not.toContain(BOOM_MARKER);

	// Observability half: the controller callback received the real error
	// (filtered to this panel — the unstable window records its own load error).
	const errors = await probe.errors(page);
	const boomErrors = errors.filter((entry) => entry.panelKind === 'fixture-boom');
	expect(boomErrors).toHaveLength(1);
	expect(boomErrors[0]).toMatchObject({
		windowId: ids.boom,
		panelKind: 'fixture-boom'
	});
	expect(boomErrors[0].message).toContain(BOOM_MARKER);

	// Retry re-mounts fresh: the fixture succeeds on attempt 2.
	await failed.getByTestId('panel-render-retry').click();
	await expect(boomHost.locator('[data-testid="panel-boom"]')).toHaveText('Boom recovered on attempt 2');
	await expect(boomHost.locator('[data-testid="panel-render-failed"]')).toHaveCount(0);
	expect(await probe.boomAttempts(page)).toBe(2);

	// Neighbors unaffected throughout.
	const alphaFrame = page.locator(`[role="dialog"][data-window-id="${ids.alpha}"]`);
	await expect(alphaFrame.locator('[data-testid="panel-alpha"]')).toBeVisible();
	await alphaFrame.locator(`[data-click-count="${MARKER_ALPHA}"]`).click();
	expect(await probe.clicks(page)).toEqual({ [MARKER_ALPHA]: 1 });
	const prefsHost = page.locator(`.mai-panel-host[data-window-id="${ids.global}"]`);
	await expect(prefsHost.locator('[data-testid="panel-prefs"]')).toBeVisible();
});

test('unmount releases the subscription fixture exactly once on close and on hide', async ({ page }) => {
	const ids = await gotoProbe(page);
	const alphaFrame = page.locator(`[role="dialog"][data-window-id="${ids.alpha}"]`);
	const cloneHost = page.locator(`.mai-panel-host[data-window-id="${CLONE_WINDOW_ID}"]`);

	// Mounted once each (frame alpha + standalone clone); nothing released yet.
	await expect(alphaFrame.locator('[data-testid="panel-alpha"]')).toBeVisible();
	await expect(cloneHost.locator('[data-testid="panel-alpha"]')).toBeVisible();
	let mounts = await probe.mounts(page);
	expect(mountCount(mounts, 'alpha', ids.alpha)).toBe(1);
	expect(mountCount(mounts, 'alpha', CLONE_WINDOW_ID)).toBe(1);
	expect(await probe.releases(page)).toEqual([]);

	// Close path: the frame's close is a controller intent; the controller
	// closes in the real state layer; the host unmounts with the window.
	await alphaFrame.locator('button[aria-label="Close"]').click();
	await expect(alphaFrame).toHaveCount(0);
	expect(await probe.closeIntents(page)).toEqual([ids.alpha]);
	expect((await probe.snapshot(page)).windows).toHaveLength(3);
	expect(await probe.releases(page)).toEqual([ids.alpha]);

	// Hide path: minimized without keep_mounted_when_hidden unmounts the
	// content and releases exactly once more (fresh mount cycle).
	await probe.setClonePresentation(page, 'minimized');
	await expect(cloneHost).toHaveAttribute('data-hidden', 'true');
	await expect(cloneHost.locator('[data-testid="panel-alpha"]')).toHaveCount(0);
	expect(await probe.releases(page)).toEqual([ids.alpha, CLONE_WINDOW_ID]);

	// Restore remounts with a fresh live context; no release churn.
	await probe.setClonePresentation(page, 'normal');
	await expect(cloneHost).toHaveAttribute('data-hidden', 'false');
	await expect(cloneHost.locator('[data-testid="panel-alpha"]')).toBeVisible();
	expect(await probe.releases(page)).toEqual([ids.alpha, CLONE_WINDOW_ID]);

	// keep_mounted_when_hidden=true keeps content mounted through minimize:
	// the descriptor presentation flips, the host must not unmount.
	const keepHost = page.locator(`.mai-panel-host[data-window-id="${KEEP_WINDOW_ID}"]`);
	await expect(keepHost).toHaveAttribute('data-hidden', 'false');
	await probe.setKeepPresentation(page, 'minimized');
	await expect(keepHost).toHaveAttribute('data-hidden', 'false');
	await expect(keepHost.locator('[data-testid="panel-prefs"]')).toBeVisible();
	expect(await probe.releases(page)).toEqual([ids.alpha, CLONE_WINDOW_ID]);

	mounts = await probe.mounts(page);
	expect(mountCount(mounts, 'alpha', ids.alpha)).toBe(1);
	// initial + one restore remount; the hide teardown released, never doubled.
	expect(mountCount(mounts, 'alpha', CLONE_WINDOW_ID)).toBe(2);
	expect(mountCount(mounts, 'prefs', ids.global)).toBe(1);
	expect(mountCount(mounts, 'prefs', KEEP_WINDOW_ID)).toBe(1);
});

test('the composed open path checks registry scope before state and reuses singletons', async ({
	page
}) => {
	const ids = await gotoProbe(page);

	// Scope-object rejection happens before any state exists.
	const disallowed = await probe.openPanel(page, 'fixture-alpha', serverDocScopeA, OPEN_GEOMETRY);
	expect(disallowed).toEqual({ stage: 'scope', ok: false, code: 'scope_object_kind_not_allowed' });
	const unknown = await probe.openPanel(page, 'log-viewer', globalScope, OPEN_GEOMETRY);
	expect(unknown).toEqual({ stage: 'scope', ok: false, code: 'unknown_panel_kind' });

	// The state layer independently rejects kinds outside the registry set.
	const rawUnknown = await probe.openRaw(page, 'log-viewer', globalScope, OPEN_GEOMETRY);
	expect(rawUnknown).toEqual({ ok: false, code: 'unknown_panel_kind' });

	// Singleton policy through the real state layer: reuse, same window id.
	const reuse = await probe.openPanel(page, 'fixture-global', globalScope, OPEN_GEOMETRY);
	expect(reuse).toMatchObject({ stage: 'state', ok: true, created: false, windowId: ids.global });
	const snapshot = await probe.snapshot(page);
	expect(snapshot.focused_id).toBe(ids.global);

	// Non-singleton kinds open a fresh instance per scope.
	const second = await probe.openPanel(page, 'fixture-alpha', serverScopeB, OPEN_GEOMETRY);
	expect(second).toMatchObject({ stage: 'state', ok: true, created: true });
	expect((await probe.snapshot(page)).windows).toHaveLength(5);
});

test('panel load failure shows the safe retry view; retry re-invokes the loader', async ({ page }) => {
	const ids = await gotoProbe(page);
	const unstableHost = page.locator(`.mai-panel-host[data-window-id="${ids.unstable}"]`);

	// First loader call rejects: the host shows the safe load-failure view and
	// reports the real error to the controller callback.
	await expect(unstableHost.locator('[data-testid="panel-load-failed"] > p')).toHaveText(
		'This panel could not be loaded.'
	);
	const loadErrors = (await probe.errors(page)).filter(
		(entry) => entry.panelKind === 'fixture-unstable'
	);
	expect(loadErrors).toHaveLength(1);
	expect(loadErrors[0].windowId).toBe(ids.unstable);
	expect(loadErrors[0].message).toContain('transient fixture load failure');

	// Retry re-invokes the loader; the second call resolves and mounts.
	await unstableHost.locator('[data-testid="panel-load-retry"]').click();
	await expect(unstableHost.locator('[data-testid="panel-prefs"]')).toBeVisible();
	await expect(unstableHost.locator('[data-testid="panel-load-failed"]')).toHaveCount(0);
	expect(await probe.unstableAttempts(page)).toBe(2);
});
