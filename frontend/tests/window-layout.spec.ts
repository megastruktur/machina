import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-window-layout — real-browser proofs for same-browser layout persistence
 * and safe restore (windowed-workspace ADR §1 D7, §5 SSR isolation, §8
 * serialization rules, §10.1 PT6, §10.2 NT3/NT4, §11 good/bad fixtures;
 * contracts.md C11, C08 rule 5).
 *
 * Exercise, against the reviewed 01-web component-host fixture and the
 * integrated producer modules (types/registry/state/presentation/drag):
 *  - persistence writes exactly the schema-v1 whitelist of the effective
 *    layout, keyed by the effective revision; no-op transitions never churn
 *    the stored document; the store's initial echo is not persisted (start
 *    before restore cannot clobber the stored document with an empty one);
 *  - PT6: a real page reload restores the stored layout — same panel kinds,
 *    pinned scopes and geometry, reapplied minimized/maximized presentations,
 *    preserved stacking order, focus on the topmost visible window, fresh
 *    view identities (window_id is session-local view identity, §3.1);
 *  - NT4/§11 negative controls: unparsable JSON, unknown schema major, bad
 *    envelope shape, unknown panel kind, server scope without server_id,
 *    non-finite geometry, forbidden title content, duplicate window_id and
 *    smuggled unknown fields are discarded/dropped with notice-safe reasons —
 *    the desktop stays functional, nothing is imported, and NOT ONE network
 *    request is issued by any restore (no submission path exists);
 *  - NT3: offscreen stored coordinates in a smaller viewport restore with the
 *    ≥64×32 visible title-bar invariant; sizes respect the per-entry
 *    `min_geometry` floor and the viewport cap;
 *  - unauthorized/deleted scope objects and registry-invalid scopes
 *    (disabled kind, undeclared scope kind, undeclared object pin) drop only
 *    their own entry; the authorization callback sees only shape-valid
 *    parsed scopes;
 *  - session changes: logout and identity changes clear the stored layout; a
 *    mismatched bound session restores nothing; the same session reloads;
 *  - scoped-controller hygiene: independent compositions are isolated
 *    (last-writer-wins on the shared document), persistence failures are
 *    contained, restore runs once (no duplicate windows), singleton instance
 *    policy holds through restore, and persistence degrades to a no-op
 *    without storage.
 *
 * The harness is the same self-contained pattern as the reviewed
 * window-state/window-types specs: an in-memory Vite production build
 * (`write: false`, virtual entry) bundling the real modules + the reviewed
 * fixture; Playwright route interception serves it; no backend, no temp
 * files, no committed fixture changes. Probe plumbing uses a spec-unique
 * `window.__maiLayoutApi` global (no Window-augmentation collision with the
 * sibling specs).
 */

const FIXTURE_URL = '/mai-window-layout-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const VIRTUAL_ID = 'virtual:mai-window-layout-entry';

const LAYOUT = `${FRONTEND_ROOT}src/lib/workspace/layout.ts`;
const STATE = `${FRONTEND_ROOT}src/lib/workspace/state.ts`;
const PRESENTATION = `${FRONTEND_ROOT}src/lib/workspace/presentation.ts`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;

/** Minimal bundle shape the harness consumes (same as sibling specs). */
type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

/** Descriptor shape across the CDP boundary (schema-v1 whitelist). */
interface ProbeDescriptor {
	window_id: string;
	panel_kind: string;
	scope: { kind: string; server_id?: string; object?: { kind: string; id: string } };
	geometry: { x: number; y: number; width: number; height: number };
	presentation: string;
	z: number;
	title_override: string | null;
}

interface ProbeDrop {
	index: number;
	code: string;
	message: string;
}

interface ProbeReport {
	restored: ProbeDescriptor[];
	dropped: ProbeDrop[];
	discarded: string | null;
	empty: boolean;
	skipped: boolean;
}

interface ProbeSnapshot {
	revision: number;
	windows: ProbeDescriptor[];
	focused_id: string | null;
}

interface ProbePersist {
	ok: boolean;
	persisted?: boolean;
	reason?: string;
}

interface ProbeCreateConfig {
	session?: string | null;
	withoutPresentation?: boolean;
	withoutStorage?: boolean;
	failingStorage?: boolean;
	denyServerIds?: string[];
	denyObjects?: Array<{ kind: string; id: string }>;
}

/** Shape the virtual entry exposes on `window` for the probes below. */
interface WindowLayoutProbeApi {
	surface(): string[];
	create(config: ProbeCreateConfig): number;
	open(
		index: number,
		request: unknown
	): { ok: boolean; descriptor?: ProbeDescriptor; issue?: { code: string; message: string } };
	move(
		index: number,
		windowId: string,
		geometry: unknown
	): { ok: boolean; value?: ProbeDescriptor; issue?: { code: string } };
	focus(index: number, windowId: string): boolean;
	minimize(index: number, windowId: string): boolean;
	toggleMaximize(index: number, windowId: string): boolean;
	restore(index: number): ProbeReport;
	start(index: number): boolean;
	persistNow(index: number): ProbePersist;
	sessionChange(index: number, sessionId: string | null): string;
	effective(index: number): ProbeSnapshot;
	scopeCalls(index: number): string[];
	storageRead(key: string): string | null;
	storageWrite(key: string, value: string): void;
	storageRemove(key: string): void;
	selectedGet(index: number): string | null;
	selectedSet(index: number, id: string): { ok: boolean; persisted?: boolean; reason?: string };
	clearStoredLayout(index: number): void;
}

declare global {
	interface Window {
		__maiLayoutApi?: WindowLayoutProbeApi;
	}
}

let harnessJs = '';
let harnessCss = '';

/**
 * Virtual entry mounted in the browser: thin wrappers over the real modules.
 * The registry fixture and deny-list policy live in-browser — callbacks
 * cannot cross the CDP boundary.
 */
function entrySource(): string {
	return `
import { mount } from 'svelte';
import ComponentHost from ${JSON.stringify(HOST)};
import * as layoutModule from ${JSON.stringify(LAYOUT)};
import * as workspaceState from ${JSON.stringify(STATE)};
import * as workspacePresentation from ${JSON.stringify(PRESENTATION)};
import * as workspaceRegistry from ${JSON.stringify(REGISTRY)};

const COMPONENT = ComponentHost;

// Registry fixture — validated entries in-browser (chat pins run objects,
// model-settings is a global singleton, server-profile opens unpinned server
// scope with a raised min size, archive is registered-but-disabled). The
// disabled entry proves the restore-time registry check (ADR §7.2 opening);
// "log-viewer" stays unregistered as the §11 unavailable-panel fixture.
const ENTRIES = [
	{
		panel_kind: 'chat',
		title: 'Chat',
		scope_types: ['server'],
		scope_object_kinds: ['run', 'conversation'],
		component: async () => COMPONENT,
		min_geometry: { x: 0, y: 0, width: 420, height: 280 },
		default_geometry: { x: 120, y: 80, width: 720, height: 480 }
	},
	{
		panel_kind: 'model-settings',
		title: 'Model settings',
		scope_types: ['global'],
		component: async () => COMPONENT,
		singleton: true
	},
	{
		panel_kind: 'server-profile',
		title: 'Server profile',
		scope_types: ['server'],
		component: async () => COMPONENT,
		min_geometry: { x: 0, y: 0, width: 500, height: 300 }
	},
	{
		panel_kind: 'archive',
		title: 'Archive',
		scope_types: ['server'],
		component: async () => COMPONENT,
		enabled: false
	}
];

const compositions = [];
const scopeCalls = [];

window.__maiLayoutApi = {
	surface: () => Object.keys(layoutModule).sort(),
	create: (config) => {
		const index = compositions.length;
		const registryResult = workspaceRegistry.createPanelRegistry(ENTRIES);
		if (!registryResult.ok) throw new Error('registry fixture invalid: ' + registryResult.issue.code);
		const registry = registryResult.value;
		const state = workspaceState.createWorkspaceState({
			knownPanelKinds: registry.knownPanelKinds(),
			instanceKey: (kind, scope) => {
				const entry = registry.entry(kind);
				return entry ? workspaceRegistry.singletonInstanceKey(kind, entry, scope) : null;
			}
		});
		const presentation = config.withoutPresentation
			? null
			: workspacePresentation.createPresentationController(state);
		const deniedKeys = [
			...(config.denyServerIds ?? []).map((id) => 'server:' + id),
			...(config.denyObjects ?? []).map((object) => 'object:' + object.kind + ':' + object.id)
		];
		const scopeCallIndex = scopeCalls.length;
		scopeCalls.push([]);
		const failingStorage = config.failingStorage
			? {
					getItem: () => null,
					setItem: () => { throw new Error('storage quota exceeded'); },
					removeItem: () => {}
				}
			: null;
		const persistence = layoutModule.createLayoutPersistence({
			state,
			presentation: presentation ?? undefined,
			registry,
			storage: config.withoutStorage ? null : failingStorage ?? undefined,
			session: config.session,
			isScopeAuthorized: (scope) => {
				scopeCalls[scopeCallIndex].push(JSON.stringify(scope));
				if (scope.kind !== 'server') return true;
				if (deniedKeys.includes('server:' + scope.server_id)) return false;
				if (
					scope.object &&
					deniedKeys.includes('object:' + scope.object.kind + ':' + scope.object.id)
				) {
					return false;
				}
				return true;
			}
		});
		compositions.push({ state, presentation, persistence });
		return index;
	},
	open: (index, request) => {
		const outcome = compositions[index].state.open(request);
		if (!outcome.ok) return { ok: false, issue: outcome.issue };
		return { ok: true, descriptor: outcome.descriptor };
	},
	move: (index, windowId, geometry) => {
		const outcome = compositions[index].state.move(windowId, geometry);
		if (!outcome.ok) return { ok: false, issue: outcome.issue };
		return { ok: true, value: outcome.value };
	},
	focus: (index, windowId) => compositions[index].state.focus(windowId),
	minimize: (index, windowId) =>
		compositions[index].presentation
			? compositions[index].presentation.minimize(windowId)
			: false,
	toggleMaximize: (index, windowId) =>
		compositions[index].presentation
			? compositions[index].presentation.toggleMaximize(windowId)
			: false,
	restore: (index) => compositions[index].persistence.restore(),
	start: (index) => {
		compositions[index].persistence.start();
		return true;
	},
	persistNow: (index) => compositions[index].persistence.persistNow(),
	sessionChange: (index, sessionId) => compositions[index].persistence.handleSessionChange(sessionId),
	effective: (index) => {
		const composition = compositions[index];
		return composition.presentation
			? composition.presentation.snapshot()
			: composition.state.snapshot();
	},
	scopeCalls: (index) => [...scopeCalls[index]],
	storageRead: (key) => window.localStorage.getItem(key),
	storageWrite: (key, value) => window.localStorage.setItem(key, value),
	storageRemove: (key) => window.localStorage.removeItem(key),
	selectedGet: (index) => compositions[index].persistence.getSelectedServerId(),
	selectedSet: (index, id) => compositions[index].persistence.setSelectedServerId(id),
	clearStoredLayout: (index) => compositions[index].persistence.clearStoredLayout()
};

mount(ComponentHost, { target: document.getElementById('app'), props: { label: 'MAI window layout probe' } });
`;
}

/** Serves the virtual entry from the in-memory bundle; nothing hits disk. */
function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-window-layout-harness',
		resolveId(id) {
			if (id === VIRTUAL_ID) return `\0${VIRTUAL_ID}`;
			return undefined;
		},
		load(id) {
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
		plugins: [svelte(), harnessPlugin(entrySource())],
		build: {
			write: false,
			emptyOutDir: true,
			outDir: 'node_modules/.mai-window-layout-fixture',
			rollupOptions: {
				input: VIRTUAL_ID,
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
	if (!harnessJs) {
		throw new Error('harness bundle incomplete: missing JS chunk');
	}
});

test.beforeEach(async ({ page }) => {
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI window-layout fixture</title>
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
		if (pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (pathname.endsWith('demo.css')) {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
	});
});

/**
 * Network requests outside the served fixture (browsers may probe favicons —
 * browser chrome, not page behavior). Restore must add none of these.
 */
function trackForeignRequests(page: Page): string[] {
	const foreign: string[] = [];
	page.on('request', (request) => {
		const url = request.url();
		if (!url.startsWith('http')) return;
		if (new URL(url).pathname.endsWith('favicon.ico')) return;
		if (!url.startsWith(FIXTURE_URL)) foreign.push(url);
	});
	return foreign;
}

/** Navigates and waits until the entry module has exposed the probe API. */
async function gotoProbe(page: Page): Promise<void> {
	await page.goto(FIXTURE_URL);
	// The entry is a deferred `<script type="module">`: under full-suite CPU
	// contention it can execute after `goto` resolves, so a bare `evaluate`
	// races it. waitForFunction retries — same readiness contract as the
	// reviewed sibling harnesses.
	await page.waitForFunction(() => window.__maiLayoutApi !== undefined, undefined, {
		timeout: 15_000
	});
}

/** Creates a composition (registry + state + presentation + persistence). */
async function create(page: Page, config: ProbeCreateConfig = {}): Promise<number> {
	return page.evaluate((cfg) => window.__maiLayoutApi!.create(cfg), config);
}

/** Opens a window in the indexed composition. */
async function openWindow(page: Page, index: number, request: unknown) {
	return page.evaluate(
		([idx, req]) => window.__maiLayoutApi!.open(idx, req),
		[index, request] as const
	);
}

/** Reads the effective (persisted-view) snapshot of the composition. */
async function effectiveOf(page: Page, index: number): Promise<ProbeSnapshot> {
	return page.evaluate((idx) => window.__maiLayoutApi!.effective(idx), index);
}

/** Reads a raw storage value through the real browser storage. */
async function storageRead(page: Page, key: string): Promise<string | null> {
	return page.evaluate((storageKey) => window.__maiLayoutApi!.storageRead(storageKey), key);
}

/** Writes a raw storage value (hostile-payload injection). */
async function storageWrite(page: Page, key: string, value: string): Promise<void> {
	await page.evaluate(
		([storageKey, value]) => window.__maiLayoutApi!.storageWrite(storageKey, value),
		[key, value]
	);
}

/** Parses the stored layout document at the boundary, or fails the test. */
function requireDocument(raw: string | null): {
	schema_version: number;
	revision: number;
	windows: ProbeDescriptor[];
} {
	if (raw === null) throw new Error('no layout document in storage');
	return JSON.parse(raw) as { schema_version: number; revision: number; windows: ProbeDescriptor[] };
}

/** Narrows an open outcome to the accepted shape (boundary type guard). */
function requireOpen(outcome: {
	ok: boolean;
	descriptor?: ProbeDescriptor;
	issue?: { code: string };
}): ProbeDescriptor {
	if (!outcome.ok || !outcome.descriptor) {
		throw new Error(`open was rejected: ${outcome.issue?.code}`);
	}
	return outcome.descriptor;
}

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'd2f6a9c0-3b4e-4f5a-8c1d-2e3f4a5b6c7d';
const RUN_ID_A = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';
const RUN_ID_B = '99e7c2f0-8d1a-4b2c-9e3f-4a5b6c7d8e9f';

const GEOMETRY_A = { x: 120, y: 80, width: 720, height: 480 };
const GEOMETRY_B = { x: 40, y: 40, width: 640, height: 400 };

const LAYOUT_KEY = 'mai.workspace.layout.v1';
const SESSION_KEY = 'mai.workspace.session.v1';
const SELECTED_KEY = 'mai.workspace.selected-server.v1';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Stored server-scoped chat pinned to a run (ADR §3.2, §8 example). */
function chatOn(
	windowId: string,
	serverId: string,
	runId: string,
	geometry = GEOMETRY_A,
	extra: Record<string, unknown> = {}
) {
	return {
		schema_version: 1,
		window_id: windowId,
		panel_kind: 'chat',
		scope: { kind: 'server', server_id: serverId, object: { kind: 'run', id: runId } },
		geometry,
		presentation: 'normal',
		z: 1,
		title_override: null,
		...extra
	};
}

test('probe mounts in the reviewed component-host; module surface is the factory + storage keys only; importing writes no storage', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await gotoProbe(page);

	// Real client mount in the reviewed fixture (not static HTML).
	await expect(page.getByTestId('component-host')).toHaveAttribute('data-mounted', 'true');

	// Module surface: the factory plus the two storage-key constants — no
	// module-level store, no SSR singleton, no hidden mutation target.
	const surface = await page.evaluate(() => window.__maiLayoutApi!.surface());
	expect(surface).toEqual([
		'LAYOUT_STORAGE_KEY',
		'SELECTED_SERVER_STORAGE_KEY',
		'SESSION_STORAGE_KEY',
		'createLayoutPersistence'
	]);

	// ADR §5 rule 4: no storage access at import/module-evaluation time.
	expect(await storageRead(page, LAYOUT_KEY)).toBeNull();
	expect(await storageRead(page, SESSION_KEY)).toBeNull();

	expect(errors).toEqual([]);
});

test('persistence writes the schema-v1 whitelist of the effective layout; no-op transitions never churn the document', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const index = await create(page);
	// Persistence starts first (the echo is swallowed); the opens then write
	// the document through real transitions.
	await page.evaluate((idx) => window.__maiLayoutApi!.start(idx), index);
	const chat = requireOpen(
		await openWindow(page, index, {
			panel_kind: 'chat',
			scope: { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } },
			geometry: GEOMETRY_A,
			title_override: 'Ingest run'
		})
	);
	requireOpen(
		await openWindow(page, index, {
			panel_kind: 'model-settings',
			scope: { kind: 'global' },
			geometry: GEOMETRY_B
		})
	);
	// The settings window is the focused top window (opened last).
	const settings = (await effectiveOf(page, index)).windows[1];

	const doc = requireDocument(await storageRead(page, LAYOUT_KEY));
	expect(doc.schema_version).toBe(1);
	expect(doc.windows).toHaveLength(2);

	// Exactly the frozen §8 field whitelist — nothing else survives.
	for (const window of doc.windows) {
		expect(Object.keys(window).sort()).toEqual([
			'geometry',
			'panel_kind',
			'presentation',
			'scope',
			'title_override',
			'window_id',
			'z'
		]);
	}
	const effective = await effectiveOf(page, index);
	expect(doc.revision).toBe(effective.revision);
	expect(doc.windows.map((window) => window.panel_kind)).toEqual(['chat', 'model-settings']);
	expect(doc.windows[0].scope).toEqual({
		kind: 'server',
		server_id: SERVER_A,
		object: { kind: 'run', id: RUN_ID_A }
	});
	expect(doc.windows[0].title_override).toBe('Ingest run');
	expect(doc.windows[1].scope).toEqual({ kind: 'global' });

	// A real geometry transition updates the document and its revision.
	const moved = await page.evaluate(
		([idx, id]) => window.__maiLayoutApi!.move(idx, id, { x: 500, y: 80, width: 720, height: 480 }),
		[index, chat.window_id] as const
	);
	expect(moved.ok).toBe(true);
	const afterMove = requireDocument(await storageRead(page, LAYOUT_KEY));
	expect(afterMove.windows[0].geometry).toEqual({ x: 500, y: 80, width: 720, height: 480 });
	expect(afterMove.revision).toBeGreaterThan(doc.revision);

	// A no-op (focusing the already-focused top window) emits no snapshot:
	// the stored document is byte-identical afterwards.
	const rawAfterMove = await storageRead(page, LAYOUT_KEY);
	const focused = await page.evaluate(
		([idx, id]) => window.__maiLayoutApi!.focus(idx, id),
		[index, settings.window_id] as const
	);
	expect(focused).toBe(true);
	expect(await storageRead(page, LAYOUT_KEY)).toBe(rawAfterMove);

	expect(errors).toEqual([]);
});

test('PT6 reload: restores pinned scopes, geometry, presentations, stacking; fresh view identities; focus on topmost visible', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const index = await create(page);
	const chat = requireOpen(
		await openWindow(page, index, {
			panel_kind: 'chat',
			scope: { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } },
			geometry: GEOMETRY_A
		})
	);
	const settings = requireOpen(
		await openWindow(page, index, {
			panel_kind: 'model-settings',
			scope: { kind: 'global' },
			geometry: GEOMETRY_B
		})
	);

	// Persistence starts first (echo swallowed); the presentation transitions
	// then write the effective layout document.
	await page.evaluate((idx) => window.__maiLayoutApi!.start(idx), index);
	expect(
		await page.evaluate(
			([idx, id]) => window.__maiLayoutApi!.minimize(idx, id),
			[index, chat.window_id] as const
		)
	).toBe(true);
	expect(
		await page.evaluate(
			([idx, id]) => window.__maiLayoutApi!.toggleMaximize(idx, id),
			[index, settings.window_id] as const
		)
	).toBe(true);

	const before = requireDocument(await storageRead(page, LAYOUT_KEY));
	expect(before.windows.map((window) => window.panel_kind)).toEqual(['chat', 'model-settings']);
	expect(before.windows.map((window) => window.presentation)).toEqual(['minimized', 'maximized']);

	// Real browser reload — the PT6 scenario (close/reload restores).
	await page.reload();
	await page.waitForFunction(() => window.__maiLayoutApi !== undefined, undefined, {
		timeout: 15_000
	});

	const restoredIndex = await create(page);
	const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), restoredIndex);
	expect(report.skipped).toBe(false);
	expect(report.discarded).toBeNull();
	expect(report.dropped).toEqual([]);

	const after = await effectiveOf(page, restoredIndex);
	expect(after.windows).toHaveLength(2);
	expect(after.windows.map((window) => window.panel_kind)).toEqual(['chat', 'model-settings']);
	// Pinned scopes survive exactly (the reconnect identity, NT6).
	expect(after.windows[0].scope).toEqual(before.windows[0].scope);
	// Geometry survives (inside the default viewport — no clamping needed).
	expect(after.windows.map((window) => window.geometry)).toEqual(
		before.windows.map((window) => window.geometry)
	);
	// Stored presentations are reapplied through their owner.
	expect(after.windows.map((window) => window.presentation)).toEqual(['minimized', 'maximized']);
	// Stacking order and z survive; focus is the topmost VISIBLE window.
	expect(after.windows.map((window) => window.z)).toEqual([1, 2]);
	expect(after.focused_id).toBe(after.windows[1].window_id);
	// View identity is fresh (session-local, §3.1) but well-formed.
	expect(after.windows[0].window_id).not.toBe(before.windows[0].window_id);
	expect(after.windows.every((window) => UUID_RE.test(window.window_id))).toBe(true);

	// The sanitize write adopted the restored layout: same fresh ids.
	const rewritten = requireDocument(await storageRead(page, LAYOUT_KEY));
	expect(rewritten.windows.map((window) => window.window_id)).toEqual(
		after.windows.map((window) => window.window_id)
	);
	expect(rewritten.revision).toBeGreaterThan(0);

	expect(errors).toEqual([]);
});

test('NT4 whole-document discards: unparsable JSON, unknown schema major, bad shape — empty workspace, storage cleared, zero requests', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	const foreign = trackForeignRequests(page);
	await gotoProbe(page);
	const baseline = foreign.length;

	const cases: Array<[string, string, string]> = [
		['not JSON', '{definitely not json', 'unparsable_json'],
		[
			'unknown major',
			JSON.stringify({ schema_version: 2, revision: 1, windows: [] }),
			'unknown_schema_version'
		],
		[
			'string version',
			JSON.stringify({ schema_version: '1', revision: 1, windows: [] }),
			'unknown_schema_version'
		],
		[
			'bad shape',
			JSON.stringify({ schema_version: 1, revision: 1, windows: 'nope' }),
			'bad_layout_shape'
		]
	];

	for (const [label, payload, expectedCode] of cases) {
		await storageWrite(page, LAYOUT_KEY, payload);
		const index = await create(page);
		const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);
		expect(report.discarded, label).toBe(expectedCode);
		expect(report.empty, label).toBe(true);
		expect(report.restored, label).toEqual([]);
		// Default workspace, functional desktop.
		expect((await effectiveOf(page, index)).windows, label).toEqual([]);
		// Discard means discard: the unusable document is removed, and a
		// discard does not write a replacement document.
		expect(await storageRead(page, LAYOUT_KEY), label).toBeNull();
	}
	// Not one request beyond the fixture itself: restore has no submission
	// path and no dynamic import of data-borne targets.
	expect(foreign.length).toBe(baseline);
	expect(errors).toEqual([]);
});

test('NT4/§11 per-entry drops: bad fixtures drop alone, survivors restore, secrets and smuggled fields never surface', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	const foreign = trackForeignRequests(page);
	await gotoProbe(page);
	const baseline = foreign.length;

	const good = chatOn('0f0c2b1e-7a4b-4c9d-8e2f-3a5b7c9d1e3f', SERVER_A, RUN_ID_A);
	const stored = {
		schema_version: 1,
		revision: 5,
		windows: [
			good,
			{ ...good, window_id: '11111111-2222-4333-8444-555555555555', panel_kind: 'log-viewer' },
			{
				...good,
				window_id: '22222222-3333-4444-8555-666666666666',
				scope: { kind: 'server' }
			},
			{
				...good,
				window_id: '33333333-4444-4555-8666-777777777777',
				geometry: { x: 1, y: 2, width: 1e999, height: 480 }
			},
			{
				...good,
				window_id: '44444444-5555-4666-8777-888888888888',
				title_override: 'password: hunter2secret'
			},
			// Duplicate window_id: deterministic keep-first dedup.
			chatOn(good.window_id, SERVER_A, RUN_ID_A, GEOMETRY_B),
			// Unknown fields smuggled into a valid entry are ignored forever.
			{
				...good,
				window_id: '66666666-7777-4888-8999-a00000000001',
				component: 'http://evil.example/x.js',
				cancelRun: 'submitJob()'
			}
		]
	};
	await storageWrite(page, LAYOUT_KEY, JSON.stringify(stored));

	const index = await create(page);
	const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);

	expect(report.discarded).toBeNull();
	expect(report.dropped.map((drop) => [drop.index, drop.code])).toEqual([
		[1, 'unknown_panel_kind'],
		[2, 'scope_server_without_server_id'],
		[3, 'bad_geometry'],
		[4, 'forbidden_content'],
		[5, 'duplicate_window_id']
	]);
	for (const drop of report.dropped) {
		// Notice-safe: no rejected content is echoed back.
		expect(drop.message).not.toContain('hunter2secret');
		expect(drop.message).not.toContain('evil.example');
	}
	expect(report.restored).toHaveLength(2);

	const effective = await effectiveOf(page, index);
	expect(effective.windows.map((window) => window.panel_kind)).toEqual(['chat', 'chat']);
	// The whitelist survives the restore: smuggled fields are gone from live
	// state.
	expect(Object.keys(effective.windows[1]).sort()).toEqual([
		'geometry',
		'panel_kind',
		'presentation',
		'scope',
		'title_override',
		'window_id',
		'z'
	]);

	// The sanitize rewrite carries none of the hostile content.
	const rewritten = (await storageRead(page, LAYOUT_KEY)) ?? '';
	expect(rewritten).not.toContain('log-viewer');
	expect(rewritten).not.toContain('hunter2secret');
	expect(rewritten).not.toContain('evil.example');
	expect(rewritten).not.toContain('cancelRun');

	// Zero foreign requests: drops never import, fetch or submit.
	expect(foreign.length).toBe(baseline);
	expect(errors).toEqual([]);
});

test('NT3 restore clamp: smaller viewport keeps the ≥64×32 title bar; per-entry min floor and viewport cap apply', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);
	await page.setViewportSize({ width: 800, height: 500 });

	const stored = {
		schema_version: 1,
		revision: 2,
		windows: [
			// Far offscreen bottom-right, size inside the viewport: only the
			// position clamps.
			chatOn('aaaaaaaa-1111-4222-8333-444444444444', SERVER_A, RUN_ID_A, {
				x: 5000,
				y: 4000,
				width: 720,
				height: 480
			}),
			// Far offscreen top-left, below its entry min size (500×300).
			// server-profile opens unpinned server scope (no object kinds).
			{
				...chatOn('aaaaaaaa-1111-4222-8333-444444444445', SERVER_B, RUN_ID_B, {
					x: -2000,
					y: -1000,
					width: 200,
					height: 150
				}),
				panel_kind: 'server-profile',
				scope: { kind: 'server', server_id: SERVER_B }
			}
		]
	};
	await storageWrite(page, LAYOUT_KEY, JSON.stringify(stored));

	const index = await create(page);
	const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);
	expect(report.dropped).toEqual([]);
	expect(report.restored).toHaveLength(2);

	const [first, second] = report.restored;
	// Sizes: kept within the viewport; the server-profile window is floored
	// at its registry min 500×300. Positions: the exact NT3 clamp results.
	expect(first.geometry).toEqual({ x: 736, y: 468, width: 720, height: 480 });
	expect(second.geometry).toEqual({ x: -436, y: 0, width: 500, height: 300 });

	// NT3 invariant recomputed from live geometry + live viewport: every
	// restored window keeps a visible, hit-testable 64×32 title-bar strip.
	for (const window of [first, second]) {
		const visibleWidth =
			Math.min(window.geometry.x + window.geometry.width, 800) - Math.max(window.geometry.x, 0);
		const visibleHeight = Math.min(window.geometry.y + 32, 500) - Math.max(window.geometry.y, 0);
		expect(visibleWidth).toBeGreaterThanOrEqual(64);
		expect(visibleHeight).toBeGreaterThanOrEqual(32);
	}
	expect(errors).toEqual([]);
});

test('unauthorized scope objects and registry-invalid scopes drop only their entry; authorization sees parsed scopes only', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const stored = {
		schema_version: 1,
		revision: 1,
		windows: [
			chatOn('aaaaaaaa-0000-4000-8000-000000000001', SERVER_A, RUN_ID_A),
			// Deleted server: the domain denies the whole server id.
			chatOn('aaaaaaaa-0000-4000-8000-000000000002', SERVER_B, RUN_ID_A),
			// Revoked object: the domain denies the pinned run.
			chatOn('aaaaaaaa-0000-4000-8000-000000000003', SERVER_A, RUN_ID_B),
			// Parse-level bad entry: must never reach the authorization callback.
			chatOn('aaaaaaaa-0000-4000-8000-000000000004', SERVER_A, RUN_ID_A, GEOMETRY_A, {
				panel_kind: 'log-viewer'
			})
		]
	};
	await storageWrite(page, LAYOUT_KEY, JSON.stringify(stored));

	const index = await create(page, {
		denyServerIds: [SERVER_B],
		denyObjects: [{ kind: 'run', id: RUN_ID_B }]
	});
	const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);
	expect(report.dropped.map((drop) => [drop.index, drop.code])).toEqual([
		[1, 'scope_not_authorized'],
		[2, 'scope_not_authorized'],
		[3, 'unknown_panel_kind']
	]);
	expect(report.restored).toHaveLength(1);
	expect(report.restored[0].scope).toEqual({
		kind: 'server',
		server_id: SERVER_A,
		object: { kind: 'run', id: RUN_ID_A }
	});

	// The authorization callback saw exactly the three shape-valid parsed
	// scopes (the allowed one included, returning true) — never the
	// parse-rejected entry.
	const calls = await page.evaluate((idx) => window.__maiLayoutApi!.scopeCalls(idx), index);
	expect(calls).toHaveLength(3);
	expect(calls).toContain(
		JSON.stringify({ kind: 'server', server_id: SERVER_B, object: { kind: 'run', id: RUN_ID_A } })
	);
	expect(calls).toContain(
		JSON.stringify({ kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_B } })
	);
	expect(calls).toContain(
		JSON.stringify({ kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } })
	);

	// Registry-invalid scopes drop their entries too: disabled kind, scope
	// kind the entry does not declare, undeclared object pin.
	const invalid = {
		schema_version: 1,
		revision: 1,
		windows: [
			{
				...chatOn('bbbbbbbb-0000-4000-8000-000000000001', SERVER_A, RUN_ID_A),
				panel_kind: 'archive'
			},
			{
				...chatOn('bbbbbbbb-0000-4000-8000-000000000002', SERVER_A, RUN_ID_A),
				scope: { kind: 'global' }
			},
			{
				...chatOn('bbbbbbbb-0000-4000-8000-000000000003', SERVER_A, RUN_ID_A),
				panel_kind: 'server-profile'
			},
			chatOn('bbbbbbbb-0000-4000-8000-000000000004', SERVER_A, RUN_ID_A)
		]
	};
	await storageWrite(page, LAYOUT_KEY, JSON.stringify(invalid));
	const second = await create(page);
	const invalidReport = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), second);
	expect(invalidReport.dropped.map((drop) => [drop.index, drop.code])).toEqual([
		[0, 'panel_disabled'],
		[1, 'scope_kind_not_allowed'],
		[2, 'scope_object_kind_not_allowed']
	]);
	expect(invalidReport.restored).toHaveLength(1);
	expect(errors).toEqual([]);
});

test('session changes: logout clears, identity change clears, same session reloads, mismatched session restores nothing', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	// Factory-time binding writes the marker; persistence starts first, then
	// the open writes the document through a real transition.
	const index = await create(page, { session: 'sess-1' });
	await page.evaluate((idx) => window.__maiLayoutApi!.start(idx), index);
	requireOpen(
		await openWindow(page, index, {
			panel_kind: 'chat',
			scope: { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } },
			geometry: GEOMETRY_A
		})
	);
	expect(await storageRead(page, SESSION_KEY)).toBe('sess-1');
	expect(await storageRead(page, LAYOUT_KEY)).not.toBeNull();

	// Logout: both the document and the marker are gone.
	const logout = await page.evaluate(
		(idx) => window.__maiLayoutApi!.sessionChange(idx, null),
		index
	);
	expect(logout).toBe('cleared');
	expect(await storageRead(page, LAYOUT_KEY)).toBeNull();
	expect(await storageRead(page, SESSION_KEY)).toBeNull();

	// A new identity writes its own marker; its layout restores after reload.
	const second = await create(page, { session: 'sess-2' });
	expect(await storageRead(page, SESSION_KEY)).toBe('sess-2');
	await page.evaluate((idx) => window.__maiLayoutApi!.start(idx), second);
	requireOpen(
		await openWindow(page, second, {
			panel_kind: 'model-settings',
			scope: { kind: 'global' },
			geometry: GEOMETRY_B
		})
	);

	await page.reload();
	await page.waitForFunction(() => window.__maiLayoutApi !== undefined, undefined, {
		timeout: 15_000
	});

	// Same identity: the layout restores.
	const sameIndex = await create(page, { session: 'sess-2' });
	const sameReport = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), sameIndex);
	expect(sameReport.skipped).toBe(false);
	expect(sameReport.restored).toHaveLength(1);
	expect(sameReport.restored[0].panel_kind).toBe('model-settings');

	// Different identity: nothing restores, the foreign document is cleared.
	const otherIndex = await create(page, { session: 'sess-3' });
	const otherReport = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), otherIndex);
	expect(otherReport.skipped).toBe(false);
	expect(otherReport.empty).toBe(true);
	expect(otherReport.restored).toEqual([]);
	expect(await storageRead(page, LAYOUT_KEY)).toBeNull();
	expect(await storageRead(page, SESSION_KEY)).toBe('sess-3');
	expect(errors).toEqual([]);
});

test('start before restore persists nothing (echo swallowed); restore runs once and writes the sanitized layout', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const index = await create(page);
	await page.evaluate((idx) => window.__maiLayoutApi!.start(idx), index);
	// The subscription's current-value echo must not clobber the stored
	// document with an empty workspace before restore reads it.
	await storageWrite(
		page,
		LAYOUT_KEY,
		JSON.stringify({
			schema_version: 1,
			revision: 3,
			windows: [chatOn('cccccccc-0000-4000-8000-000000000001', SERVER_A, RUN_ID_A)]
		})
	);

	const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);
	expect(report.skipped).toBe(false);
	expect(report.restored).toHaveLength(1);

	// Restore ran exactly once: a second call is a no-op, no duplicate window.
	const second = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);
	expect(second.skipped).toBe(true);
	expect(second.restored).toEqual([]);
	expect((await effectiveOf(page, index)).windows).toHaveLength(1);

	// The stored document is the sanitized live layout (fresh view id).
	const rewritten = requireDocument(await storageRead(page, LAYOUT_KEY));
	expect(rewritten.windows).toHaveLength(1);
	expect(rewritten.windows[0].window_id).not.toBe('cccccccc-0000-4000-8000-000000000001');
	expect(errors).toEqual([]);
});

test('singleton instance policy holds through restore: duplicate stored singletons collapse to one live window', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const singletonEntry = {
		schema_version: 1,
		window_id: 'dddddddd-0000-4000-8000-00000000000a',
		panel_kind: 'model-settings',
		scope: { kind: 'global' },
		geometry: GEOMETRY_B,
		presentation: 'normal',
		z: 1,
		title_override: null
	};
	await storageWrite(
		page,
		LAYOUT_KEY,
		JSON.stringify({
			schema_version: 1,
			revision: 4,
			windows: [
				singletonEntry,
				{ ...singletonEntry, window_id: 'dddddddd-0000-4000-8000-00000000000b' }
			]
		})
	);

	const index = await create(page);
	const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);
	// Nothing is dropped — the frozen instance policy reuses the live
	// instance, so the second stored singleton is not duplicated.
	expect(report.dropped).toEqual([]);
	expect(report.restored).toHaveLength(1);
	expect((await effectiveOf(page, index)).windows).toHaveLength(1);
	expect(errors).toEqual([]);
});

test('persistence failures are contained: throwing storage never breaks transitions, persistNow reports the failure', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const index = await create(page, { failingStorage: true });
	const outcome = await page.evaluate((idx) => window.__maiLayoutApi!.persistNow(idx), index);
	expect(outcome.ok).toBe(false);
	expect(outcome.reason).toContain('storage quota exceeded');

	// The desktop is unaffected: opens keep succeeding while persistence
	// reports its contained failure.
	const open = await openWindow(page, index, {
		panel_kind: 'chat',
		scope: { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } },
		geometry: GEOMETRY_A
	});
	expect(open.ok).toBe(true);
	expect((await effectiveOf(page, index)).windows).toHaveLength(1);
	expect(errors).toEqual([]);
});

test('without storage the controller degrades to no-ops (SSR/blocked-storage shape, base-state source)', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const index = await create(page, { withoutStorage: true, withoutPresentation: true });
	const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);
	expect(report).toEqual({ restored: [], dropped: [], discarded: null, empty: true, skipped: false });
	const persisted = await page.evaluate((idx) => window.__maiLayoutApi!.persistNow(idx), index);
	expect(persisted).toEqual({ ok: true, persisted: false });
	const changed = await page.evaluate(
		(idx) => window.__maiLayoutApi!.sessionChange(idx, 'sess-1'),
		index
	);
	expect(changed).toBe('unchanged');
	// Windows still open (base-state source works without a presentation
	// controller); nothing was ever written.
	requireOpen(
		await openWindow(page, index, {
			panel_kind: 'model-settings',
			scope: { kind: 'global' },
			geometry: GEOMETRY_B
		})
	);
	expect(await storageRead(page, LAYOUT_KEY)).toBeNull();
	expect(errors).toEqual([]);
});

test('two compositions are isolated and last-writer-wins on the shared document', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const first = await create(page);
	const second = await create(page);
	await page.evaluate((idx) => window.__maiLayoutApi!.start(idx), first);
	await page.evaluate((idx) => window.__maiLayoutApi!.start(idx), second);

	const firstChat = requireOpen(
		await openWindow(page, first, {
			panel_kind: 'chat',
			scope: { kind: 'server', server_id: SERVER_A, object: { kind: 'run', id: RUN_ID_A } },
			geometry: GEOMETRY_A
		})
	);
	requireOpen(
		await openWindow(page, second, {
			panel_kind: 'model-settings',
			scope: { kind: 'global' },
			geometry: GEOMETRY_B
		})
	);

	// Last writer wins on the shared storage (ADR §5 rule 6).
	let doc = requireDocument(await storageRead(page, LAYOUT_KEY));
	expect(doc.windows.map((window) => window.panel_kind)).toEqual(['model-settings']);

	// A transition in the first composition takes the document back over.
	const moved = await page.evaluate(
		([idx, id]) => window.__maiLayoutApi!.move(idx, id, { x: 200, y: 100, width: 720, height: 480 }),
		[first, firstChat.window_id] as const
	);
	expect(moved.ok).toBe(true);
	doc = requireDocument(await storageRead(page, LAYOUT_KEY));
	expect(doc.windows.map((window) => window.panel_kind)).toEqual(['chat']);
	expect(doc.windows[0].geometry).toEqual({ x: 200, y: 100, width: 720, height: 480 });
	expect(errors).toEqual([]);
});

test('restore reapplies stored presentations deterministically: minimized stays minimized, maximized stays maximized, focus on the topmost visible window', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	// Stored layout: chat minimized, second chat maximized (schema-v1 single
	// presentation values, exactly what the persistence layer writes).
	await storageWrite(
		page,
		LAYOUT_KEY,
		JSON.stringify({
			schema_version: 1,
			revision: 7,
			windows: [
				{
					...chatOn('eeeeeeee-0000-4000-8000-000000000001', SERVER_A, RUN_ID_A, GEOMETRY_A),
					presentation: 'minimized'
				},
				{
					...chatOn('eeeeeeee-0000-4000-8000-000000000002', SERVER_A, RUN_ID_B, GEOMETRY_B),
					presentation: 'maximized',
					z: 2
				}
			]
		})
	);

	const index = await create(page);
	const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);
	expect(report.dropped).toEqual([]);
	expect(report.restored.map((window) => window.presentation)).toEqual(['minimized', 'maximized']);

	const effective = await effectiveOf(page, index);
	expect(effective.windows.map((window) => window.presentation)).toEqual(['minimized', 'maximized']);
	// Focus falls to the topmost VISIBLE window (the maximized one).
	expect(effective.focused_id).toBe(effective.windows[1].window_id);
	expect(errors).toEqual([]);
});

// --- W2: selected-server preference (same session lifecycle as windows) ----

test('selected server: canonical UUID persists, non-UUID and garbage are rejected or repaired', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const index = await create(page);
	const set = await page.evaluate(
		([idx, id]) => window.__maiLayoutApi!.selectedSet(idx, id),
		[index, SERVER_A] as const
	);
	expect(set).toEqual({ ok: true, persisted: true });
	// The stored preference is the BARE canonical UUID string — no envelope,
	// no server object, no profile fields.
	expect(await storageRead(page, SELECTED_KEY)).toBe(SERVER_A);
	expect(await page.evaluate((idx) => window.__maiLayoutApi!.selectedGet(idx), index)).toBe(
		SERVER_A
	);

	// The layout document itself stays untouched by selection writes.
	expect(await storageRead(page, LAYOUT_KEY)).toBeNull();

	// Non-UUID values (profile-like objects, junk) are refused, never stored.
	const reject = await page.evaluate(
		([idx, id]) => window.__maiLayoutApi!.selectedSet(idx, id),
		[index, JSON.stringify({ server_id: SERVER_A, name: 'Alpha', ssh: {} })] as const
	);
	expect(reject.ok).toBe(false);
	expect(reject.persisted).toBeUndefined();
	expect(await storageRead(page, SELECTED_KEY)).toBe(SERVER_A);

	// Pre-seeded hostile/stale values read as absent AND are removed on read.
	for (const hostile of [
		'not-a-uuid',
		JSON.stringify({ server_id: SERVER_A }),
		SERVER_A.toUpperCase(),
		''
	]) {
		await storageWrite(page, SELECTED_KEY, hostile);
		expect(await page.evaluate((idx) => window.__maiLayoutApi!.selectedGet(idx), index)).toBeNull();
		expect(await storageRead(page, SELECTED_KEY)).toBeNull();
	}

	// Backwards compatibility: an old document with no selection key still
	// restores its windows; the selection reads as absent, not an error.
	await storageWrite(
		page,
		LAYOUT_KEY,
		JSON.stringify({
			schema_version: 1,
			revision: 3,
			windows: [
				{
					window_id: '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a99',
					panel_kind: 'model-settings',
					scope: { kind: 'global' },
					geometry: GEOMETRY_A,
					presentation: 'normal',
					z: 1,
					title_override: null
				}
			]
		})
	);
	const report = await page.evaluate((idx) => window.__maiLayoutApi!.restore(idx), index);
	expect(report.restored).toHaveLength(1);
	expect(await page.evaluate((idx) => window.__maiLayoutApi!.selectedGet(idx), index)).toBeNull();
	expect(errors).toEqual([]);
});

test('selected server clears together with the layout on identity change and clearStoredLayout; without storage it no-ops', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await gotoProbe(page);

	const index = await create(page, { session: 'sess-1' });
	await page.evaluate(
		([idx, id]) => window.__maiLayoutApi!.selectedSet(idx, id),
		[index, SERVER_B] as const
	);
	expect(await storageRead(page, SELECTED_KEY)).toBe(SERVER_B);

	// A different identity clears the selection TOGETHER with the document —
	// a server id from another session can never restore.
	await page.evaluate((idx) => window.__maiLayoutApi!.sessionChange(idx, 'sess-2'), index);
	expect(await storageRead(page, SELECTED_KEY)).toBeNull();

	// Re-seed, then clearStoredLayout: same combined clearing.
	await page.evaluate(
		([idx, id]) => window.__maiLayoutApi!.selectedSet(idx, id),
		[index, SERVER_A] as const
	);
	expect(await storageRead(page, SELECTED_KEY)).toBe(SERVER_A);
	await page.evaluate((idx) => window.__maiLayoutApi!.clearStoredLayout(idx), index);
	expect(await storageRead(page, SELECTED_KEY)).toBeNull();
	expect(await storageRead(page, LAYOUT_KEY)).toBeNull();
	// clearStoredLayout removes the marker too (pre-existing contract).
	expect(await storageRead(page, SESSION_KEY)).toBeNull();

	// Rebind the identity (writes the marker again), then re-seed: the same
	// identity keeps the selection across a reload (same-session restore).
	await page.evaluate((idx) => window.__maiLayoutApi!.sessionChange(idx, 'sess-2'), index);
	expect(await storageRead(page, SESSION_KEY)).toBe('sess-2');

	// Same identity keeps the selection across a reload (same-session restore).
	await page.evaluate(
		([idx, id]) => window.__maiLayoutApi!.selectedSet(idx, id),
		[index, SERVER_A] as const
	);
	await page.reload();
	await page.waitForFunction(() => window.__maiLayoutApi !== undefined, undefined, {
		timeout: 15_000
	});
	const sameIndex = await create(page, { session: 'sess-2' });
	expect(await page.evaluate((idx) => window.__maiLayoutApi!.selectedGet(idx), sameIndex)).toBe(
		SERVER_A
	);

	// No storage: both operations degrade to a safe no-op.
	const storageless = await create(page, { withoutStorage: true });
	expect(
		await page.evaluate((idx) => window.__maiLayoutApi!.selectedGet(idx), storageless)
	).toBeNull();
	const noop = await page.evaluate(
		([idx, id]) => window.__maiLayoutApi!.selectedSet(idx, id),
		[storageless, SERVER_A] as const
	);
	expect(noop).toEqual({ ok: true, persisted: false });
	expect(errors).toEqual([]);
});
