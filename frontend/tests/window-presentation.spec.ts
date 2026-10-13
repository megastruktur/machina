import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-window-presentation — real-browser proofs for the shared presentation
 * transitions (minimize/restore/maximize) and the taskbar/window switcher
 * (frontend/src/lib/workspace/presentation.ts + WindowSwitcher.svelte).
 *
 * Focused per the test policy: the controller runs in real Chromium against
 * the reviewed 01-web component fixture (tests/fixtures/component-host.svelte)
 * — no backend, no invented API. Harness convention is the sibling specs': an
 * IN-MEMORY Vite production build of two virtual modules (a rune-free fixture
 * composition around the real `createWorkspaceState` +
 * `createPresentationController` + the real `WindowFrame`/`WindowSwitcher`,
 * and a mount entry), served through route interception.
 *
 * Scenario coverage (brief acceptance 1–3; windowed-workspace ADR §7.2
 * lifecycle, §10.1 PT4, §10.2 NT6, §6 taskbar a11y; runtime-scenarios WS-PT4;
 * contracts.md C08 rule 5, C11):
 *  - module surface: the only runtime export is the controller factory —
 *    no module-level store, no second window-state copy;
 *  - factory isolation: two controllers over two base states share nothing
 *    (per-session instances, no SSR singleton);
 *  - minimize/restore via the taskbar path: hidden window keeps geometry,
 *    scope, identity and stacking (view-only), remembers its pre-minimize
 *    presentation (maximized comes back maximized), restore focuses (PT4);
 *  - minimize of the focused window moves focus to the topmost remaining
 *    visible window; all-hidden leaves the effective focus null;
 *  - maximize is the ADR §7.2 presentation overlay: geometry never moves,
 *    focus never moves, the frame flips `data-presentation` and drops the
 *    resize handles only while maximized;
 *  - presentation transitions read no domain channel (smuggled-field spy
 *    stays unread), run no release hooks (close does, exactly once), and
 *    the composed journey performs zero network attempts (NT6: no domain
 *    mutation can be expressed through this surface);
 *  - close prunes hidden windows from the view and the taskbar (PT5); a
 *    reopened window starts `normal` (no stale presentation);
 *  - composed a11y: `role="toolbar"` `aria-label="Open windows"`, entries
 *    named "«panel title» — «server label»"/"Global", minimized conveyed by
 *    `aria-pressed`, keyboard-reachable and operable.
 *
 * Known limits (recorded in the card evidence): DesktopShell wiring and the
 * maximized overlay's viewport CSS belong to 01-workspace-runtime; restore
 * of a persisted presentation across reload belongs to 01-window-layout.
 */

const FIXTURE_URL = '/mai-window-presentation-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const APP_ID = 'virtual:mai-window-presentation-app.svelte';
const ENTRY_ID = 'virtual:mai-window-presentation-entry.js';

const FRAME = `${FRONTEND_ROOT}src/lib/workspace/WindowFrame.svelte`;
const SWITCHER = `${FRONTEND_ROOT}src/lib/workspace/WindowSwitcher.svelte`;
const STATE = `${FRONTEND_ROOT}src/lib/workspace/state.ts`;
const PRESENTATION = `${FRONTEND_ROOT}src/lib/workspace/presentation.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'd2f6a9c0-3b4e-4f5a-8c1d-2e3f4a5b6c7d';
const RUN_ID_A = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';
const RUN_ID_B = '99e7c2f0-8d1a-4b2c-9e3f-4a5b6c7d8e9f';

const GEOMETRY_A = { x: 40, y: 40, width: 480, height: 320 };
const GEOMETRY_B = { x: 620, y: 140, width: 360, height: 260 };

/**
 * Registry stand-ins the probes open against (same convention as the
 * sibling specs; "log-viewer" stays absent — ADR §11 unregistered kind).
 * The request SHAPES are built in-browser by the probe api (openChat/
 * openPrefs/openChatSpy) because serialized page.evaluate callbacks cannot
 * close over node helpers; the geometry constants below are the assertion
 * counterparts of the in-browser builders.
 */
const REGISTRY_KINDS = ['chat', 'model-settings'];

/**
 * Rune-free fixture composition (virtual .svelte modules compile in legacy
 * mode inside this in-memory production build — sibling-spec convention):
 * the thin future-workspace-runtime role. Frame chrome intents and taskbar
 * activation map 1:1 onto the real controller/state transitions; hidden
 * windows render nothing (the default `keep_mounted_when_hidden: false`
 * policy at the composition layer, ADR §7.1) while their taskbar entries
 * remain and carry `aria-pressed`.
 */
const APP_SOURCE = `
<script>
	import WindowFrame from '${FRAME}';
	import WindowSwitcher from '${SWITCHER}';
	import { createWorkspaceState } from '${STATE}';
	import { createPresentationController } from '${PRESENTATION}';
	import ComponentHost from '${HOST}';
	import { writable, derived } from 'svelte/store';

	const api = window.__maiPresentationApi;

	const state = createWorkspaceState({
		knownPanelKinds: new Set(${JSON.stringify(REGISTRY_KINDS)}),
		instanceKey: () => null
	});
	const presentation = createPresentationController(state);

	const chat = state.open({
		panel_kind: 'chat',
		scope: { kind: 'server', server_id: '${SERVER_A}', object: { kind: 'run', id: '${RUN_ID_A}' } },
		geometry: { x: ${GEOMETRY_A.x}, y: ${GEOMETRY_A.y}, width: ${GEOMETRY_A.width}, height: ${GEOMETRY_A.height} }
	});
	const prefs = state.open({
		panel_kind: 'model-settings',
		scope: { kind: 'global' },
		geometry: { x: ${GEOMETRY_B.x}, y: ${GEOMETRY_B.y}, width: ${GEOMETRY_B.width}, height: ${GEOMETRY_B.height} }
	});
	if (!chat.ok || !prefs.ok) throw new Error('fixture opens must validate');

	const titles = {};
	titles[chat.descriptor.window_id] = 'Chat';
	titles[prefs.descriptor.window_id] = 'Preferences';
	const openOrder = {};
	openOrder[chat.descriptor.window_id] = 0;
	openOrder[prefs.descriptor.window_id] = 1;

	api.windowIds = { chat: chat.descriptor.window_id, global: prefs.descriptor.window_id };
	api.ui = {
		minimize: (id) => presentation.minimize(id),
		restore: (id) => presentation.restore(id),
		toggleMaximize: (id) => presentation.toggleMaximize(id),
		activate: (id) => presentation.activate(id),
		close: (id) => state.close(id),
		snapshot: () => presentation.snapshot(),
		baseSnapshot: () => state.snapshot()
	};

	function contextLabelFor(descriptor) {
		return descriptor.scope.kind === 'global' ? 'Global' : 'Server Alpha';
	}

	const effective = writable(presentation.snapshot());
	presentation.subscribe((next) => effective.set(next));

	// Diagnostic journal of every effective transition (test triage input,
	// mirrored by probe.errors for uncaught page errors).
	api.journal = [];
	presentation.subscribe((next) => {
		api.journal.push(
			next.revision +
				' focused=' + (next.focused_id ?? 'null') +
				' ' +
				next.windows.map((w) => w.panel_kind + ':' + w.presentation).join(',')
		);
	});

	// Derived views as store chains: no reactive-statement syntax, so the
	// virtual module compiles under the project's runes-mode compiler while
	// the template keeps $store auto-subscriptions (sibling-spec convention).
	// Stable DOM order (open order): hiding/raising must not reparent the
	// remaining subtrees; stacking stays with descriptor.z.
	const ordered = derived(effective, (view) =>
		[...view.windows].sort(
			(a, b) => (openOrder[a.window_id] ?? 0) - (openOrder[b.window_id] ?? 0)
		)
	);
	const entries = derived(effective, (view) =>
		view.windows.map((descriptor) => ({
			window_id: descriptor.window_id,
			title: titles[descriptor.window_id] ?? descriptor.panel_kind,
			context_label: contextLabelFor(descriptor),
			minimized: descriptor.presentation === 'minimized',
			focused: view.focused_id === descriptor.window_id
		}))
	);
	// Hidden windows render nothing (the default keep_mounted_when_hidden:
	// false policy at the composition layer, ADR §7.1); their taskbar
	// entries remain.
	const visible = derived(ordered, (windows) =>
		windows.filter((descriptor) => descriptor.presentation !== 'minimized')
	);
</script>

<ComponentHost label="MAI window presentation fixture">
	<div
		class="mai-desktop"
		data-testid="desktop"
		style="position: relative; width: 1000px; height: 560px;"
	>
		{#each $visible as descriptor (descriptor.window_id)}
			<!-- Frame activation = the focus intent (raise visible only,
			     never restore: a minimize control click bubbles an activation
			     after the transition; see presentation.ts wiring rule). -->
			<WindowFrame
				{descriptor}
				title={titles[descriptor.window_id] ?? descriptor.panel_kind}
				contextLabel={contextLabelFor(descriptor)}
				focused={$effective.focused_id === descriptor.window_id}
				onactivate={(id) => {
					api.journal.push('evt:frame-activate:' + id);
					presentation.focus(id);
				}}
				onminimize={(id) => {
					api.journal.push('evt:frame-minimize:' + id);
					presentation.minimize(id);
				}}
				ontogglemaximize={(id) => {
					api.journal.push('evt:frame-maximize:' + id);
					presentation.toggleMaximize(id);
				}}
				onclose={(id) => {
					state.close(id);
				}}
				ongeometry={(id, geometry) => {
					const result = state.move(id, geometry);
					if (!result.ok) throw new Error('move rejected: ' + result.issue.code);
				}}
			>
				<div class="sample" data-testid={'sample-' + descriptor.panel_kind}>
					<p data-sample-text={descriptor.panel_kind}>
						{titles[descriptor.window_id]} inert sample body — mounted only while visible.
					</p>
					<button type="button" data-sample-button={descriptor.panel_kind}>
						{titles[descriptor.window_id]} action
					</button>
				</div>
			</WindowFrame>
		{/each}
	</div>
	<WindowSwitcher
		entries={$entries}
		onactivate={(id) => {
			api.journal.push('evt:switcher-activate:' + id);
			presentation.activate(id);
		}}
	/>
</ComponentHost>
`;

const ENTRY_SOURCE = `
import { mount } from 'svelte';
import App from '${APP_ID}';
import '${TOKENS}';
import * as presentationModule from '${PRESENTATION}';
import { createWorkspaceState } from '${STATE}';

const instances = [];
const hookLogs = [];
const spyReads = [];

const probe = {
	windowIds: null,
	ui: null,
	/** Uncaught page errors — the composed surface must never throw. */
	errors: [],
	surface: () => Object.keys(presentationModule).sort(),
	create: () => {
		const index = instances.length;
		const state = createWorkspaceState({
			knownPanelKinds: new Set(${JSON.stringify(REGISTRY_KINDS)}),
			instanceKey: () => null
		});
		const presentation = presentationModule.createPresentationController(state);
		instances.push({ state, presentation });
		hookLogs.push([]);
		spyReads.push(0);
		return index;
	},
	// Request builders live in-browser: node fixture constants cannot be
	// closed over by serialized page.evaluate callbacks.
	openChat: (index, serverId, runId) =>
		instances[index].state.open({
			panel_kind: 'chat',
			scope: { kind: 'server', server_id: serverId, object: { kind: 'run', id: runId } },
			geometry: { x: ${GEOMETRY_A.x}, y: ${GEOMETRY_A.y}, width: ${GEOMETRY_A.width}, height: ${GEOMETRY_A.height} }
		}),
	openPrefs: (index) =>
		instances[index].state.open({
			panel_kind: 'model-settings',
			scope: { kind: 'global' },
			geometry: { x: ${GEOMETRY_B.x}, y: ${GEOMETRY_B.y}, width: ${GEOMETRY_B.width}, height: ${GEOMETRY_B.height} }
		}),
	// Opens the chat request with a smuggled extra field backed by a
	// read-counting getter — a would-be domain channel. The count stays 0
	// iff no presentation transition ever reads it (NT6). hookLabel
	// registers a close-time release hook on the opened window.
	openChatSpy: (index, serverId, runId, hookLabel) => {
		let reads = 0;
		const smuggled = {
			panel_kind: 'chat',
			scope: { kind: 'server', server_id: serverId, object: { kind: 'run', id: runId } },
			geometry: { x: ${GEOMETRY_A.x}, y: ${GEOMETRY_A.y}, width: ${GEOMETRY_A.width}, height: ${GEOMETRY_A.height} },
			get cancelRun() {
				reads += 1;
				return 'domain-cancel-invocation';
			}
		};
		const outcome = instances[index].state.open(smuggled);
		spyReads[index] = reads;
		if (!outcome.ok) return outcome;
		if (hookLabel) outcome.release(() => hookLogs[index].push('release:' + hookLabel));
		return { ok: true, descriptor: outcome.descriptor };
	},
	spyReads: (index) => spyReads[index],
	minimize: (index, id) => instances[index].presentation.minimize(id),
	restore: (index, id) => instances[index].presentation.restore(id),
	toggleMaximize: (index, id) => instances[index].presentation.toggleMaximize(id),
	focus: (index, id) => instances[index].presentation.focus(id),
	activate: (index, id) => instances[index].presentation.activate(id),
	focusBase: (index, id) => instances[index].state.focus(id),
	close: (index, id) => instances[index].state.close(id),
	snapshot: (index) => instances[index].presentation.snapshot(),
	baseSnapshot: (index) => instances[index].state.snapshot(),
	hookLog: (index) => [...hookLogs[index]]
};

window.__maiPresentationApi = probe;
window.addEventListener('error', (event) => {
	probe.errors.push(String(event.error?.message ?? event.message));
});
window.addEventListener('unhandledrejection', (event) => {
	probe.errors.push('rejection:' + String(event.reason));
});

mount(App, { target: document.getElementById('app'), props: {} });
`;

/** Descriptor shape as it crosses the CDP boundary (schema-v1 whitelist). */
interface ProbeDescriptor {
	window_id: string;
	panel_kind: string;
	presentation: string;
	z: number;
	scope: { kind: string };
	geometry: { x: number; y: number; width: number; height: number };
}

interface ProbeSnapshot {
	revision: number;
	windows: ProbeDescriptor[];
	focused_id: string | null;
}

interface ProbeOpen {
	ok: boolean;
	descriptor?: ProbeDescriptor;
	issue?: { code: string; message: string };
}

interface UiApi {
	minimize(id: string): boolean;
	restore(id: string): boolean;
	toggleMaximize(id: string): boolean;
	activate(id: string): boolean;
	close(id: string): boolean;
	snapshot(): ProbeSnapshot;
	baseSnapshot(): ProbeSnapshot;
}

interface ProbeApi {
	windowIds: { chat: string; global: string } | null;
	ui: UiApi | null;
	/** Effective-transition log (composed instance) for failure triage. */
	journal: string[];
	/** Uncaught page errors — must stay empty on every journey. */
	errors: string[];
	surface(): string[];
	create(): number;
	openChat(index: number, serverId: string, runId: string): ProbeOpen;
	openPrefs(index: number): ProbeOpen;
	openChatSpy(index: number, serverId: string, runId: string, hookLabel?: string): ProbeOpen;
	spyReads(index: number): number;
	minimize(index: number, id: string): boolean;
	restore(index: number, id: string): boolean;
	toggleMaximize(index: number, id: string): boolean;
	focus(index: number, id: string): boolean;
	activate(index: number, id: string): boolean;
	focusBase(index: number, id: string): boolean;
	close(index: number, id: string): boolean;
	snapshot(index: number): ProbeSnapshot;
	baseSnapshot(index: number): ProbeSnapshot;
	hookLog(index: number): string[];
}

declare global {
	interface Window {
		/** Spec-unique probe global (no collision with the sibling specs). */
		__maiPresentationApi?: ProbeApi;
	}
}

let harnessJs = '';
let harnessCss = '';

/** Serves the virtual modules from the in-memory bundle; nothing hits disk. */
function harnessPlugin(appSource: string, entrySource: string): Plugin {
	return {
		name: 'mai-window-presentation-harness',
		resolveId(id) {
			// The .svelte virtual module must NOT get the conventional `\0`
			// prefix (vite-plugin-svelte excludes `\0` ids from compilation).
			if (id === APP_ID) return id;
			if (id === ENTRY_ID) return `\0${id}`;
			return undefined;
		},
		load(id) {
			if (id === APP_ID) return appSource;
			if (id === `\0${ENTRY_ID}`) return entrySource;
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(APP_SOURCE, ENTRY_SOURCE)],
		build: {
			write: false,
			emptyOutDir: true,
			outDir: 'node_modules/.mai-window-presentation-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: { entryFileNames: 'demo.js', assetFileNames: 'demo[extname]' }
			}
		}
	});

	/** Minimal bundle shape (same structural boundary as the sibling specs). */
	type BuiltBundle = {
		output: Array<
			| { type: 'chunk'; code: string }
			| { type: 'asset'; fileName: string; source: string | Uint8Array }
		>;
	};

	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') harnessJs += item.code;
			else if (item.fileName.endsWith('.css')) harnessCss += String(item.source);
		}
	}
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete: missing JS chunk or CSS asset');
	}
});

/**
 * Off-fixture request aborts, counted per test page by the route handler
 * below (the NT6 zero-network negative control).
 */
const offFixtureCounts = new WeakMap<Page, { count: number }>();

test.beforeEach(async ({ page }) => {
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI window presentation fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script>
</body>
</html>`;

	// NT6 negative control: every non-fixture request is aborted and counted.
	// The composed journey must make ZERO attempts — no fetch/XHR/SSE can be
	// reached through the presentation surface. Favicon noise is exempt.
	const counter = { count: 0 };
	offFixtureCounts.set(page, counter);
	await page.route('**/*', (route) => {
		const { pathname } = new URL(route.request().url());
		if (!pathname.startsWith(FIXTURE_URL)) {
			if (!pathname.endsWith('favicon.ico')) counter.count += 1;
			return route.abort();
		}
		if (pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (pathname.endsWith('demo.css')) {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
	});
	await page.goto(FIXTURE_URL);
	// The entry is a deferred `<script type="module">`: waitForFunction
	// retries under full-suite CPU contention (sibling-spec readiness
	// contract). The composed app sets `windowIds` during mount, so this
	// also proves the real component tree mounted.
	await page.waitForFunction(
		() =>
			window.__maiPresentationApi?.windowIds !== null &&
			window.__maiPresentationApi?.windowIds !== undefined,
		undefined,
		{ timeout: 15_000 }
	);
});

/** Aborts counted for this test's page (zero expected on every journey). */
async function offFixtureRequests(page: Page): Promise<number> {
	return offFixtureCounts.get(page)?.count ?? -1;
}

async function snapshotOf(page: Page, index = 0): Promise<ProbeSnapshot> {
	return page.evaluate(([i]) => window.__maiPresentationApi!.snapshot(i!), [index] as const);
}

async function baseSnapshotOf(page: Page, index = 0): Promise<ProbeSnapshot> {
	return page.evaluate(([i]) => window.__maiPresentationApi!.baseSnapshot(i!), [index] as const);
}

function windowOf(
	snapshot: ProbeSnapshot,
	kind: string
): ProbeDescriptor {
	const found = snapshot.windows.find((window) => window.panel_kind === kind);
	if (!found) throw new Error(`no ${kind} window in snapshot`);
	return found;
}

/** A window frame locator for one panel kind (frames carry role="dialog"). */
function frame(page: Page, kind: string) {
	return page.locator(`.mai-window-frame[data-panel-kind="${kind}"]`);
}

/** The taskbar entry button for one window id. */
function entry(page: Page, windowId: string) {
	return page.locator(`[data-testid="window-switcher"] button[data-window-id="${windowId}"]`);
}

// ---------------------------------------------------------------------------
// Controller-level probes (bare instance pairs; the composed UI is a
// separate describe below).
// ---------------------------------------------------------------------------

test.describe('presentation controller', () => {
	test('exports exactly the controller factory — no module-level state', async ({ page }) => {
		const surface = await page.evaluate(() => window.__maiPresentationApi!.surface());
		expect(surface).toEqual(['createPresentationController']);
	});

	test('two controllers over two base states share nothing (factory isolation)', async ({
		page
	}) => {
		const first = await page.evaluate(() => window.__maiPresentationApi!.create());
		const second = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!), [
			first,
			SERVER_A,
			RUN_ID_A
		] as const);
		await page.evaluate(([i]) => window.__maiPresentationApi!.openPrefs(i!), [second] as const);

		const beforeSecond = await snapshotOf(page, second);
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [
			first,
			(await snapshotOf(page, first)).windows[0].window_id
		] as const);

		// The other instance is untouched: no module-level shared state.
		const afterSecond = await snapshotOf(page, second);
		expect(afterSecond).toEqual(beforeSecond);
		expect(afterSecond.windows[0].presentation).toBe('normal');
	});

	test('minimize hides view-only: geometry, scope, z and base state untouched', async ({ page }) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!),
			[index, SERVER_A, RUN_ID_A] as const
		);
		const before = await snapshotOf(page, index);
		const baseBefore = await baseSnapshotOf(page, index);

		const chatId = before.windows[0].window_id;
		const changed = await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [
			index,
			chatId
		] as const);
		expect(changed).toBe(true);

		const after = await snapshotOf(page, index);
		const chat = windowOf(after, 'chat');
		expect(chat.presentation).toBe('minimized');
		expect(chat.geometry).toEqual(before.windows[0].geometry);
		expect(chat.z).toBe(before.windows[0].z);
		// Scope stays pinned: the transition cannot retarget the window.
		expect(chat.scope).toEqual({
			kind: 'server',
			server_id: SERVER_A,
			object: { kind: 'run', id: RUN_ID_A }
		});

		// The base state is untouched: presentation is this module's dimension;
		// the persisted-layout domain still sees the base descriptor.
		const baseAfter = await baseSnapshotOf(page, index);
		expect(baseAfter.revision).toBe(baseBefore.revision);
		expect(baseAfter.windows[0]).toEqual(baseBefore.windows[0]);
		expect(baseAfter.windows[0].presentation).toBe('normal');
		// One user action = exactly one effective transition.
		expect(after.revision).toBe(before.revision + 1);
	});

	test('minimizing the focused window moves focus to the topmost visible window', async ({
		page
	}) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!),
			[index, SERVER_A, RUN_ID_A] as const
		);
		await page.evaluate(([i]) => window.__maiPresentationApi!.openPrefs(i!), [index] as const);
		const before = await snapshotOf(page, index);
		const prefsId = windowOf(before, 'model-settings').window_id;
		expect(before.focused_id).toBe(prefsId);
		const prefsBefore = windowOf(before, 'model-settings');

		await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [
			index,
			prefsId
		] as const);

		const after = await snapshotOf(page, index);
		expect(after.focused_id).toBe(windowOf(after, 'chat').window_id);
		// The hidden window keeps its geometry (view-only transition); its z
		// is stacking order and legitimately renumbers when the newly focused
		// window raises (base bounded-stacking invariant, z = index + 1).
		expect(windowOf(after, 'model-settings').geometry).toEqual(prefsBefore.geometry);
	});

	test('minimizing every window leaves the effective focus null; restore returns it', async ({
		page
	}) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!),
			[index, SERVER_A, RUN_ID_A] as const
		);
		await page.evaluate(([i]) => window.__maiPresentationApi!.openPrefs(i!), [index] as const);
		const chatId = windowOf(await snapshotOf(page, index), 'chat').window_id;
		const prefsId = windowOf(await snapshotOf(page, index), 'model-settings').window_id;

		await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [index, chatId] as const);
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [index, prefsId] as const);
		expect((await snapshotOf(page, index)).focused_id).toBeNull();

		// PT4: activating the entry restores and focuses.
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.activate(i!, id!), [index, prefsId] as const);
		const restored = await snapshotOf(page, index);
		expect(restored.focused_id).toBe(prefsId);
		expect(windowOf(restored, 'model-settings').presentation).toBe('normal');
		expect(windowOf(restored, 'chat').presentation).toBe('minimized');
	});

	test('restore returns the remembered presentation and focuses (PT4)', async ({ page }) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!),
			[index, SERVER_A, RUN_ID_A] as const
		);
		await page.evaluate(([i]) => window.__maiPresentationApi!.openPrefs(i!), [index] as const);
		const chatId = windowOf(await snapshotOf(page, index), 'chat').window_id;
		const prefsId = windowOf(await snapshotOf(page, index), 'model-settings').window_id;

		// (a) normal → minimized → normal.
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [index, chatId] as const);
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.restore(i!, id!), [index, chatId] as const);
		const chatAfter = await snapshotOf(page, index);
		expect(windowOf(chatAfter, 'chat').presentation).toBe('normal');
		expect(chatAfter.focused_id).toBe(chatId);
		expect(windowOf(chatAfter, 'chat').z).toBe(2);

		// (b) maximized → minimized → maximized (pre-minimize presentation is
		// remembered; geometry stays the original normal bounds).
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.toggleMaximize(i!, id!), [index, prefsId] as const);
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [index, prefsId] as const);
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.restore(i!, id!), [index, prefsId] as const);
		const prefsAfter = await snapshotOf(page, index);
		expect(windowOf(prefsAfter, 'model-settings').presentation).toBe('maximized');
		expect(windowOf(prefsAfter, 'model-settings').geometry).toEqual(GEOMETRY_B);
	});

	test('transitions are idempotent-safe and unknown ids are rejected', async ({ page }) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!),
			[index, SERVER_A, RUN_ID_A] as const
		);
		const chatId = windowOf(await snapshotOf(page, index), 'chat').window_id;

		await page.evaluate(([i, id]) => window.__maiPresentationApi!.toggleMaximize(i!, id!), [index, chatId] as const);
		const maximized = await snapshotOf(page, index);

		// Minimize twice: the second is a no-op and must NOT clobber the
		// remembered pre-minimize presentation.
		expect(await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [index, chatId] as const)).toBe(true);
		expect(await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [index, chatId] as const)).toBe(false);
		expect(await page.evaluate(([i, id]) => window.__maiPresentationApi!.restore(i!, id!), [index, chatId] as const)).toBe(true);
		expect(windowOf(await snapshotOf(page, index), 'chat').presentation).toBe('maximized');

		// restore on a visible window: strictly from-minimized → false.
		expect(await page.evaluate(([i, id]) => window.__maiPresentationApi!.restore(i!, id!), [index, chatId] as const)).toBe(false);
		// Unknown ids: nothing transitions, nothing emits.
		const before = await snapshotOf(page, index);
		for (const op of ['minimize', 'restore', 'toggleMaximize', 'focus', 'activate'] as const) {
			expect(
				await page.evaluate(
					([i, op, id]) => window.__maiPresentationApi![op](i!, id!),
					[index, op, '6f6e3a58-0b1c-4d2e-9f3a-5b6c7d8e9f00'] as const
				)
			).toBe(false);
		}
		expect(await snapshotOf(page, index)).toEqual(before);
		// The maximized state above came from toggle + safe double-minimize.
		expect(maximized.windows[0].presentation).toBe('maximized');
	});

	test('toggleMaximize never moves geometry, scope, stacking or focus', async ({ page }) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!),
			[index, SERVER_A, RUN_ID_A] as const
		);
		await page.evaluate(([i]) => window.__maiPresentationApi!.openPrefs(i!), [index] as const);
		const before = await snapshotOf(page, index);
		const baseBefore = await baseSnapshotOf(page, index);
		const chatId = windowOf(before, 'chat').window_id;

		expect(
			await page.evaluate(([i, id]) => window.__maiPresentationApi!.toggleMaximize(i!, id!), [index, chatId] as const)
		).toBe(true);
		const maximized = await snapshotOf(page, index);
		expect(windowOf(maximized, 'chat').presentation).toBe('maximized');
		// View-only: geometry identical, focus untouched, base revision still.
		expect(windowOf(maximized, 'chat').geometry).toEqual(GEOMETRY_A);
		expect(maximized.focused_id).toBe(before.focused_id);
		expect(await baseSnapshotOf(page, index)).toEqual(baseBefore);

		expect(
			await page.evaluate(([i, id]) => window.__maiPresentationApi!.toggleMaximize(i!, id!), [index, chatId] as const)
		).toBe(true);
		const restored = await snapshotOf(page, index);
		expect(windowOf(restored, 'chat').presentation).toBe('normal');
		expect(windowOf(restored, 'chat').geometry).toEqual(GEOMETRY_A);
	});

	test('focus raises visible windows only — it never resurrects hidden ones', async ({ page }) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!),
			[index, SERVER_A, RUN_ID_A] as const
		);
		await page.evaluate(([i]) => window.__maiPresentationApi!.openPrefs(i!), [index] as const);
		const chatId = windowOf(await snapshotOf(page, index), 'chat').window_id;

		// Frame-activation wiring on a just-minimized window must be inert:
		// a minimize control click bubbles an activation for the now-hidden
		// window, and raising must never restore it (wiring rule).
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [index, chatId] as const);
		expect(
			await page.evaluate(([i, id]) => window.__maiPresentationApi!.focus(i!, id!), [index, chatId] as const)
		).toBe(false);
		const after = await snapshotOf(page, index);
		expect(windowOf(after, 'chat').presentation).toBe('minimized');
		expect(after.focused_id).not.toBe(chatId);
		expect(after.revision).toBe(3);
	});

	test('activate restores hidden and focuses visible (PT4/PT2)', async ({ page }) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!),
			[index, SERVER_A, RUN_ID_A] as const
		);
		await page.evaluate(([i]) => window.__maiPresentationApi!.openPrefs(i!), [index] as const);
		const chatId = windowOf(await snapshotOf(page, index), 'chat').window_id;
		const prefsId = windowOf(await snapshotOf(page, index), 'model-settings').window_id;

		// Hidden → restored + focused.
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [index, chatId] as const);
		expect(
			await page.evaluate(([i, id]) => window.__maiPresentationApi!.activate(i!, id!), [index, chatId] as const)
		).toBe(true);
		expect((await snapshotOf(page, index)).focused_id).toBe(chatId);

		// Visible unfocused → focused (raised).
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.focusBase(i!, id!), [index, chatId] as const);
		expect(
			await page.evaluate(([i, id]) => window.__maiPresentationApi!.activate(i!, id!), [index, prefsId] as const)
		).toBe(true);
		expect((await snapshotOf(page, index)).focused_id).toBe(prefsId);

		// Already-focused visible → no-op.
		expect(
			await page.evaluate(([i, id]) => window.__maiPresentationApi!.activate(i!, id!), [index, prefsId] as const)
		).toBe(false);
	});

	test('transitions read no domain channel and release only on close (NT6)', async ({ page }) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		const opened = await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChatSpy(i!, s!, r!, 'spy'),
			[index, SERVER_A, RUN_ID_A] as const
		);
		expect(opened.ok).toBe(true);
		const chatId = opened.descriptor!.window_id;
		await page.evaluate(([i]) => window.__maiPresentationApi!.openPrefs(i!), [index] as const);
		const prefsId = windowOf(await snapshotOf(page, index), 'model-settings').window_id;

		// Every presentation transition leaves the smuggled domain field
		// unread and the release hook untouched.
		for (const op of ['minimize', 'restore', 'toggleMaximize', 'toggleMaximize', 'activate', 'minimize', 'activate'] as const) {
			await page.evaluate(([i, op, id]) => window.__maiPresentationApi![op](i!, id!), [
				index,
				op,
				chatId
			] as const);
		}
		expect(await page.evaluate(([i]) => window.__maiPresentationApi!.spyReads(i!), [index] as const)).toBe(0);
		expect(await page.evaluate(([i]) => window.__maiPresentationApi!.hookLog(i!), [index] as const)).toEqual([]);

		// Close while hidden: the hook runs exactly once, at close.
		expect(await page.evaluate(([i, id]) => window.__maiPresentationApi!.close(i!, id!), [index, chatId] as const)).toBe(true);
		expect(await page.evaluate(([i]) => window.__maiPresentationApi!.hookLog(i!), [index] as const)).toEqual([
			'release:spy'
		]);
		expect(await page.evaluate(([i]) => window.__maiPresentationApi!.spyReads(i!), [index] as const)).toBe(0);

		// Reopening after close starts fresh: no stale presentation leaks.
		await page.evaluate(([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!), [
			index,
			SERVER_A,
			RUN_ID_A
		] as const);
		expect(windowOf(await snapshotOf(page, index), 'chat').presentation).toBe('normal');
	});

	test('close prunes hidden windows from the view and the taskbar domain (PT5)', async ({ page }) => {
		const index = await page.evaluate(() => window.__maiPresentationApi!.create());
		await page.evaluate(
			([i, s, r]) => window.__maiPresentationApi!.openChat(i!, s!, r!),
			[index, SERVER_A, RUN_ID_A] as const
		);
		await page.evaluate(([i]) => window.__maiPresentationApi!.openPrefs(i!), [index] as const);
		const chatId = windowOf(await snapshotOf(page, index), 'chat').window_id;

		await page.evaluate(([i, id]) => window.__maiPresentationApi!.minimize(i!, id!), [index, chatId] as const);
		await page.evaluate(([i, id]) => window.__maiPresentationApi!.close(i!, id!), [index, chatId] as const);

		const after = await snapshotOf(page, index);
		expect(after.windows.some((window) => window.window_id === chatId)).toBe(false);
		expect(after.focused_id).not.toBe(chatId);
		// The dead id is inert for every transition (maps pruned, no leak).
		for (const op of ['minimize', 'restore', 'toggleMaximize', 'focus', 'activate'] as const) {
			expect(
				await page.evaluate(([i, op, id]) => window.__maiPresentationApi![op](i!, id!), [index, op, chatId] as const)
			).toBe(false);
		}
	});
});

// ---------------------------------------------------------------------------
// Composed UI probes (real WindowFrame + WindowSwitcher + controller).
// ---------------------------------------------------------------------------

test.describe('composed presentation surface', () => {
	test('taskbar renders the frozen a11y contract for two live windows', async ({ page }) => {
		const switcher = page.getByTestId('window-switcher');
		await expect(switcher).toHaveAttribute('role', 'toolbar');
		await expect(switcher).toHaveAttribute('aria-label', 'Open windows');

		const { chat, global: prefs } = await page.evaluate(
			() => window.__maiPresentationApi!.windowIds!
		);
		await expect(entry(page, chat)).toHaveAttribute('aria-pressed', 'false');
		await expect(entry(page, prefs)).toHaveAttribute('aria-pressed', 'false');
		// Accessible names identify panel + pinned context (ADR §6).
		await expect(page.getByRole('button', { name: 'Chat — Server Alpha' })).toHaveCount(1);
		await expect(page.getByRole('button', { name: 'Preferences — Global' })).toHaveCount(1);
		// The focused window's entry shows it; the other does not.
		await expect(entry(page, prefs)).toHaveAttribute('data-focused', 'true');
		await expect(entry(page, chat)).toHaveAttribute('data-focused', 'false');
	});

	test('frame minimize hides content, marks the entry and moves focus to the neighbor', async ({
		page
	}) => {
		const { chat, global: prefs } = await page.evaluate(
			() => window.__maiPresentationApi!.windowIds!
		);

		await frame(page, 'chat').getByRole('button', { name: 'Minimize' }).click();

		// PT4: the window hides; the taskbar entry remains with aria-pressed.
		await expect(
			frame(page, 'chat'),
			`journal=${JSON.stringify(await page.evaluate(() => window.__maiPresentationApi!.journal))} errors=${JSON.stringify(await page.evaluate(() => window.__maiPresentationApi!.errors))}`
		).toHaveCount(0);
		await expect(page.getByTestId('sample-chat')).toHaveCount(0);
		await expect(entry(page, chat)).toHaveAttribute('aria-pressed', 'true');
		await expect(entry(page, chat)).toBeVisible();

		// Focus fell to the topmost remaining visible window (raised).
		await expect(frame(page, 'model-settings')).toHaveAttribute('data-focused', 'true');
		await expect(frame(page, 'model-settings')).toHaveAttribute('data-presentation', 'normal');
		// The neighbor's content is untouched (NT6 view isolation).
		await expect(page.getByTestId('sample-model-settings')).toHaveCount(1);

		const effective = await page.evaluate(() => window.__maiPresentationApi!.ui!.snapshot());
		expect(windowOf(effective, 'chat').presentation).toBe('minimized');
		const base = await page.evaluate(() => window.__maiPresentationApi!.ui!.baseSnapshot());
		expect(windowOf(base, 'chat').geometry).toEqual(GEOMETRY_A);
		expect(await offFixtureRequests(page)).toBe(0);
	});

	test('taskbar activation restores and focuses; content remounts fresh (PT4)', async ({ page }) => {
		const { chat } = await page.evaluate(() => window.__maiPresentationApi!.windowIds!);

		await frame(page, 'chat').getByRole('button', { name: 'Minimize' }).click();
		await expect(frame(page, 'chat')).toHaveCount(0);

		await entry(page, chat).click();

		await expect(frame(page, 'chat')).toHaveAttribute('data-presentation', 'normal');
		await expect(frame(page, 'chat')).toHaveAttribute('data-focused', 'true');
		// Hidden windows unmount their content by default: the reopened body
		// is a fresh mount of the inert sample.
		await expect(page.getByTestId('sample-chat')).toHaveCount(1);
		await expect(entry(page, chat)).toHaveAttribute('aria-pressed', 'false');
		await expect(entry(page, chat)).toHaveAttribute('data-focused', 'true');
		// Focus moved off the neighbor.
		await expect(frame(page, 'model-settings')).toHaveAttribute('data-focused', 'false');
	});

	test('maximize is the presentation overlay: handles drop, geometry never moves', async ({
		page
	}) => {
		const { global: prefs } = await page.evaluate(() => window.__maiPresentationApi!.windowIds!);
		const prefsFrame = frame(page, 'model-settings');

		await prefsFrame.getByRole('button', { name: 'Maximize' }).click();
		await expect(prefsFrame).toHaveAttribute('data-presentation', 'maximized');
		// A maximized frame's geometry belongs to the presentation owner: the
		// pointer resize strips exist only in `normal` presentation.
		await expect(prefsFrame.locator('[data-resize-direction]')).toHaveCount(0);
		// View-only: the descriptor geometry (inline style) never moved.
		await expect
			.poll(() => prefsFrame.evaluate((el) => ({ w: el.style.width, h: el.style.height })))
			.toEqual({ w: `${GEOMETRY_B.width}px`, h: `${GEOMETRY_B.height}px` });
		// Maximize never moved focus (prefs was already focused).
		await expect(prefsFrame).toHaveAttribute('data-focused', 'true');

		// Restore via the same control (label flipped by the frame).
		await prefsFrame.getByRole('button', { name: 'Restore' }).click();
		await expect(prefsFrame).toHaveAttribute('data-presentation', 'normal');
		await expect(prefsFrame.locator('[data-resize-direction]')).toHaveCount(8);
		await expect
			.poll(() => prefsFrame.evaluate((el) => ({ w: el.style.width, h: el.style.height })))
			.toEqual({ w: `${GEOMETRY_B.width}px`, h: `${GEOMETRY_B.height}px` });
	});

	test('keyboard: taskbar entries are reachable and operable (ADR §6)', async ({ page }) => {
		const { chat, global: prefs } = await page.evaluate(
			() => window.__maiPresentationApi!.windowIds!
		);

		// Keyboard-activate the visible, unfocused chat window (PT2 half).
		await entry(page, chat).focus();
		await page.keyboard.press('Enter');
		await expect(frame(page, 'chat')).toHaveAttribute('data-focused', 'true');

		// Minimize from the frame, then restore from the taskbar by keyboard.
		await frame(page, 'chat').getByRole('button', { name: 'Minimize' }).click();
		await expect(
			entry(page, chat),
			`journal=${JSON.stringify(await page.evaluate(() => window.__maiPresentationApi!.journal))} errors=${JSON.stringify(await page.evaluate(() => window.__maiPresentationApi!.errors))}`
		).toHaveAttribute('aria-pressed', 'true');
		await entry(page, chat).focus();
		await page.keyboard.press('Enter');
		await expect(frame(page, 'chat')).toHaveCount(1);
		await expect(frame(page, 'chat')).toHaveAttribute('data-focused', 'true');
		await expect(entry(page, prefs)).toHaveAttribute('data-focused', 'false');
	});

	test('closing a hidden neighbor removes its entry and leaves the rest intact', async ({
		page
	}) => {
		const { chat, global: prefs } = await page.evaluate(
			() => window.__maiPresentationApi!.windowIds!
		);

		await frame(page, 'chat').getByRole('button', { name: 'Minimize' }).click();
		await page.evaluate(() => {
			const ids = window.__maiPresentationApi!.windowIds!;
			return window.__maiPresentationApi!.ui!.close(ids.global);
		});

		// PT5: closed window's entry is gone (even though it stayed visible).
		await expect(entry(page, prefs)).toHaveCount(0);
		await expect(frame(page, 'model-settings')).toHaveCount(0);
		// The hidden neighbor is untouched in state and on the taskbar.
		await expect(entry(page, chat)).toHaveCount(1);
		await expect(entry(page, chat)).toHaveAttribute('aria-pressed', 'true');
		const effective = await page.evaluate(() => window.__maiPresentationApi!.ui!.snapshot());
		expect(effective.windows.map((window) => window.window_id)).toEqual([chat]);
		expect(effective.focused_id).toBeNull();
	});

	test('the composed journey performs zero network attempts (NT6 fixture)', async ({ page }) => {
		const { chat, global: prefs } = await page.evaluate(
			() => window.__maiPresentationApi!.windowIds!
		);

		// Full user path through chrome controls + taskbar + close.
		await frame(page, 'chat').getByRole('button', { name: 'Minimize' }).click();
		await entry(page, chat).click();
		await frame(page, 'chat').getByRole('button', { name: 'Maximize' }).click();
		await frame(page, 'chat').getByRole('button', { name: 'Restore' }).click();
		await frame(page, 'model-settings').getByRole('button', { name: 'Minimize' }).click();
		await entry(page, prefs).click();
		await frame(page, 'model-settings').getByRole('button', { name: 'Close' }).click();

		await expect(frame(page, 'model-settings')).toHaveCount(0);
		await expect(entry(page, prefs)).toHaveCount(0);
		await expect(frame(page, 'chat')).toHaveCount(1);
		await expect(entry(page, chat)).toHaveCount(1);
		// Zero requests beyond the inert fixture — the presentation surface
		// cannot reach a domain API (no fetch/XHR/SSE/WebSocket attempt).
		expect(await offFixtureRequests(page)).toBe(0);
	});
});
