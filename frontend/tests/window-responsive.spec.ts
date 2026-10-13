import { expect, test, type Locator, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-window-responsive — real-browser proofs for the viewport reflow
 * controller (frontend/src/lib/workspace/responsive.ts).
 *
 * Focused per the test policy: the controller runs in real Chromium against
 * the reviewed 01-web component fixture (tests/fixtures/component-host.svelte)
 * — no backend, no invented API. Harness convention is the sibling specs':
 * an IN-MEMORY Vite production build of two virtual modules (a rune-free
 * fixture composition around the real `createWorkspaceState`, the real
 * `createPanelRegistry` and the real `createResponsiveController`, plus the
 * real `WindowFrame`, and a mount entry), served through route interception.
 *
 * Scenario coverage (brief "feature-gate coverage"; registry rows
 * WS-PT6/NT3 — runtime journey owner 01-verify):
 *  - NT3: a real narrowing viewport (page.setViewportSize) reflows the
 *    window whose committed geometry no longer fits so every window keeps a
 *    ≥64×32 visible, hit-testable title-bar area (measured on the real DOM),
 *    including previously offscreen coordinates
 *  - PT6 identity: window_id, pinned scope (server + object), z, focus and
 *    title survive the transition unchanged — a reflow commits through the
 *    single `move` transition only
 *  - no-op discipline: widening (and the zoom-out direction) never moves an
 *    in-bounds window and never churns the layout revision
 *  - browser zoom: zoom changes the CSS-px viewport; the zoom-in direction
 *    is proven two ways — a fractional injected viewport (integer-exact
 *    geometry through the REAL resize event, ADR §8 rule 2) and a real
 *    Chromium device-metrics override (fewer CSS px at deviceScaleFactor 2)
 *  - bounds floor: on degenerate tiny viewports the registry `min_geometry`
 *    floor wins over the viewport cap (the a11y minimum is never violated),
 *    per-entry for chat (250×180) and the frozen global default for notes
 *    (320×200) — the REAL registry is the single minimum-size source
 *  - keyboard access (brief acceptance 2): focused title bar and tab order
 *    survive the viewport transition (keyed DOM, no reparent)
 *  - listener lifecycle: stop detaches (no reflow), start reattaches, and
 *    start is idempotent (one listener per controller)
 *
 * Measurement convention (sibling-spec rule): every expected value is
 * derived from the LIVE `window.innerWidth/innerHeight` and the fixture's
 * measured desktop origin — never from hardcoded constants or Playwright's
 * nominal viewport size (scrollbars legitimately shrink the CSS-px
 * viewport; the controller and the expectations must read the same numbers).
 *
 * Known limitations (recorded in the card evidence): desktop browser zoom
 * (Ctrl +/−) fires the same `resize` stream proven here; a real
 * Ctrl-keystroke zoom cannot be produced deterministically in headless
 * Chromium, so the zoom legs use the two equivalents above. Pinch zoom on
 * touch devices is out of scope (ADR §9.2: no mandatory touch). The
 * multi-window real-browser journey (narrow/widen/zoom with persisted
 * layouts) is assembled by 01-workspace-runtime at the P01 gate.
 */

const FIXTURE_URL = '/mai-window-responsive-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const APP_ID = 'virtual:mai-window-responsive-app.svelte';
const ENTRY_ID = 'virtual:mai-window-responsive-entry.js';

const FRAME = `${FRONTEND_ROOT}src/lib/workspace/WindowFrame.svelte`;
const STATE = `${FRONTEND_ROOT}src/lib/workspace/state.ts`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const RESPONSIVE = `${FRONTEND_ROOT}src/lib/workspace/responsive.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const RUN_ID_A = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';

/** Frozen NT3 constants (types.ts TITLE_BAR_MIN — values are contractual). */
const TITLE_BAR_MIN_W = 64;
const TITLE_BAR_MIN_H = 32;
/**
 * The WindowFrame border (`--mai-border-width`, frozen 1px): the title-bar
 * hit target sits one border inside the frame box on every side — the same
 * inset responsive.ts applies (FRAME_BORDER_PX), measured live here.
 */
const FRAME_BORDER_PX = 1;
/** The registry entry minimum for the chat window (min_geometry seam). */
const CHAT_MIN_W = 250;
const CHAT_MIN_H = 180;
/** The frozen global default (DEFAULT_MIN_GEOMETRY) applies to notes. */
const DEFAULT_MIN_W = 320;
const DEFAULT_MIN_H = 200;

/**
 * Rune-free fixture composition (virtual .svelte modules compile in legacy
 * mode inside this in-memory production build — sibling-spec convention):
 * the thin future-workspace-runtime role. It wires the REAL registry and
 * REAL controller; the viewport/origin providers are the composition seams
 * the runtime fills (live window metrics, measured desktop rect).
 */
const APP_SOURCE = `
<script>
	import WindowFrame from '${FRAME}';
	import { createWorkspaceState } from '${STATE}';
	import { createPanelRegistry } from '${REGISTRY}';
	import { createResponsiveController } from '${RESPONSIVE}';
	import ComponentHost from '${HOST}';
	import { writable } from 'svelte/store';

	const api = window.__maiWindowResponsiveApi;

	const registryResult = createPanelRegistry([
		{
			panel_kind: 'chat',
			title: 'Chat',
			scope_types: ['server'],
			scope_object_kinds: ['run'],
			min_geometry: { x: 0, y: 0, width: ${CHAT_MIN_W}, height: ${CHAT_MIN_H} },
			// Registry validation requires a loader function; this fixture
			// renders WindowFrame directly and never mounts through PanelHost,
			// so a called loader would be a defect and fails loudly.
			component: () => Promise.reject(new Error('fixture never mounts panels through the host'))
		},
		{
			panel_kind: 'notes',
			title: 'Notes',
			scope_types: ['global'],
			component: () => Promise.reject(new Error('fixture never mounts panels through the host'))
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected');
	const registry = registryResult.value;

	const state = createWorkspaceState({
		knownPanelKinds: registry.knownPanelKinds(),
		instanceKey: () => null
	});

	const chat = state.open({
		panel_kind: 'chat',
		scope: { kind: 'server', server_id: '${SERVER_A}', object: { kind: 'run', id: '${RUN_ID_A}' } },
		geometry: { x: 40, y: 40, width: 480, height: 320 }
	});
	const notes = state.open({
		panel_kind: 'notes',
		scope: { kind: 'global' },
		geometry: { x: 620, y: 120, width: 360, height: 260 }
	});
	if (!chat.ok || !notes.ok) throw new Error('fixture opens must validate');
	state.focus(chat.descriptor.window_id);

	api.windowIds = { chat: chat.descriptor.window_id, notes: notes.descriptor.window_id };
	api.snapshot = () => state.snapshot();

	// Viewport/origin seams: the override stands in for browser zoom (fewer
	// or fractional CSS-px viewport units); the origin measures the desktop
	// region's viewport rect, rounded to CSS-px integers — the composed
	// runtime supplies the same measurement.
	let viewportOverride = null;
	let desktopElement = null;
	const viewport = () =>
		viewportOverride ?? { width: window.innerWidth, height: window.innerHeight };
	const origin = () => {
		if (!desktopElement) return { left: 0, top: 0 };
		const rect = desktopElement.getBoundingClientRect();
		return { left: Math.round(rect.left), top: Math.round(rect.top) };
	};
	api.setViewportOverride = (box) => {
		viewportOverride = box;
	};
	api.desktopOrigin = origin;
	api.liveViewport = () => ({ width: window.innerWidth, height: window.innerHeight });

	const controller = createResponsiveController({ state, registry, viewport, origin });
	let stopRef = null;
	api.reflow = () => controller.reflow();
	api.start = () => {
		stopRef = controller.start();
		return stopRef;
	};
	api.stop = () => {
		if (stopRef) stopRef();
		stopRef = null;
	};
	api.startTwice = () => {
		// The listener must already be attached here (beforeEach) — the pair
		// proves the module returns the PENDING stop instead of attaching a
		// second listener.
		const stop = controller.start();
		const stopAgain = controller.start();
		return { stop, stopAgain, identical: stop === stopAgain };
	};

	api.moveWindow = (kind, geometry) => {
		const id = kind === 'chat' ? chat.descriptor.window_id : notes.descriptor.window_id;
		const result = state.move(id, geometry);
		if (!result.ok) throw new Error('move rejected: ' + result.issue.code);
		return result.value.geometry;
	};

	const titles = {
		[chat.descriptor.window_id]: 'Chat',
		[notes.descriptor.window_id]: 'Notes'
	};

	// Stable DOM order (open order): a reflow must not reparent subtrees —
	// the keyboard focus proof depends on it (sibling-spec convention).
	const openOrder = {};
	openOrder[chat.descriptor.window_id] = 0;
	openOrder[notes.descriptor.window_id] = 1;

	function inOpenOrder(snap) {
		const windows = [...snap.windows].sort(
			(a, b) => (openOrder[a.window_id] ?? 0) - (openOrder[b.window_id] ?? 0)
		);
		return { revision: snap.revision, windows, focused_id: snap.focused_id };
	}

	const snapshotStore = writable(inOpenOrder(state.snapshot()));
	state.subscribe((next) => snapshotStore.set(inOpenOrder(next)));
</script>

<ComponentHost label="MAI window responsive fixture">
	<div
		class="mai-desktop"
		data-testid="desktop"
		bind:this={desktopElement}
		style="position: relative; width: 100%; height: 420px;"
	>
		{#each $snapshotStore.windows as descriptor (descriptor.window_id)}
			<WindowFrame
				{descriptor}
				title={titles[descriptor.window_id] ?? descriptor.panel_kind}
				contextLabel={descriptor.scope.kind === 'server' ? 'Server Alpha' : ''}
				focused={$snapshotStore.focused_id === descriptor.window_id}
			>
				<div class="sample" data-sample={descriptor.panel_kind}>
					<p data-sample-text={descriptor.panel_kind}>
						{titles[descriptor.window_id] ?? descriptor.panel_kind} inert sample body.
					</p>
				</div>
			</WindowFrame>
		{/each}
	</div>
</ComponentHost>
`;

const ENTRY_SOURCE = `
import { mount } from 'svelte';
import App from '${APP_ID}';
import '${TOKENS}';

const api = {
	windowIds: null,
	snapshot: null,
	reflow: null,
	start: null,
	stop: null,
	startTwice: null,
	moveWindow: null,
	setViewportOverride: null,
	desktopOrigin: null,
	liveViewport: null
};
window.__maiWindowResponsiveApi = api;

mount(App, { target: document.getElementById('app'), props: { api } });
`;

/** Minimal bundle shape (same structural boundary as the sibling specs). */
type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

interface ProbeGeometry {
	x: number;
	y: number;
	width: number;
	height: number;
}

interface ProbeBox {
	width: number;
	height: number;
}

interface ProbeOrigin {
	left: number;
	top: number;
}

interface ProbeWindow {
	window_id: string;
	panel_kind: string;
	scope: unknown;
	presentation: string;
	z: number;
	title_override: string | null;
	geometry: ProbeGeometry;
}

interface ProbeSnapshot {
	revision: number;
	windows: ProbeWindow[];
	focused_id: string | null;
}

interface ProbeApi {
	windowIds: { chat: string; notes: string } | null;
	snapshot(): ProbeSnapshot;
	reflow(): { inspected: number; adjustments: Array<{ window_id: string; from: ProbeGeometry; to: ProbeGeometry }> };
	start(): () => void;
	stop(): void;
	startTwice(): { stop: () => void; stopAgain: () => void; identical: boolean };
	moveWindow(kind: 'chat' | 'notes', geometry: ProbeGeometry): ProbeGeometry;
	setViewportOverride(box: ProbeBox | null): void;
	desktopOrigin(): ProbeOrigin;
	liveViewport(): ProbeBox;
}

declare global {
	interface Window {
		/** Spec-unique probe global (no collision with the sibling specs). */
		__maiWindowResponsiveApi?: ProbeApi;
	}
}

let harnessJs = '';
let harnessCss = '';

/** Serves the virtual modules from the in-memory bundle; nothing hits disk. */
function harnessPlugin(appSource: string, entrySource: string): Plugin {
	return {
		name: 'mai-window-responsive-harness',
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
			outDir: 'node_modules/.mai-window-responsive-fixture',
			rollupOptions: {
				input: ENTRY_ID,
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
		throw new Error('harness bundle incomplete: missing JS chunk or CSS asset');
	}
});

test.beforeEach(async ({ page }) => {
	// Deterministic base viewport (Desktop Chrome default, stated explicitly).
	await page.setViewportSize({ width: 1280, height: 720 });

	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI window responsive fixture</title>
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
		if (pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (pathname.endsWith('demo.css')) {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
	});

	await page.goto(FIXTURE_URL);
	await expect(page.getByTestId('component-host')).toHaveAttribute('data-mounted', 'true');
	// Attach the real listener once per mount; every test starts attached.
	await page.evaluate(() => window.__maiWindowResponsiveApi!.start());
});

async function snapshotOf(page: Page): Promise<ProbeSnapshot> {
	return page.evaluate(() => window.__maiWindowResponsiveApi!.snapshot());
}

async function revisionOf(page: Page): Promise<number> {
	return page.evaluate(() => window.__maiWindowResponsiveApi!.snapshot().revision);
}

async function liveViewportOf(page: Page): Promise<ProbeBox> {
	return page.evaluate(() => window.__maiWindowResponsiveApi!.liveViewport());
}

async function originOf(page: Page): Promise<ProbeOrigin> {
	return page.evaluate(() => window.__maiWindowResponsiveApi!.desktopOrigin());
}

function windowOf(snapshot: ProbeSnapshot, kind: string): ProbeWindow {
	const found = snapshot.windows.find((window) => window.panel_kind === kind);
	if (!found) throw new Error(`no ${kind} window in snapshot`);
	return found;
}

const frameOf = (page: Page, kind: string): Locator =>
	page.locator(`[role="dialog"][data-panel-kind="${kind}"]`);

/**
 * NT3 contract bounds for a reflowed window, derived from the frozen rule
 * (not from the implementation): the title-bar hit target is the bar
 * ELEMENT — descriptor box inset by the frame border on every side — so a
 * recovered window satisfies
 * 64 − barWidth − left − border ≤ x ≤ viewportWidth − 64 − left − border and
 * −top − border ≤ y ≤ viewportHeight − 32 − top − border.
 */
function expectNt3Bounds(
	geometry: ProbeGeometry,
	viewport: ProbeBox,
	origin: ProbeOrigin
): void {
	const barWidth = Math.max(TITLE_BAR_MIN_W, geometry.width - 2 * FRAME_BORDER_PX);
	expect(geometry.x).toBeGreaterThanOrEqual(
		TITLE_BAR_MIN_W - barWidth - origin.left - FRAME_BORDER_PX
	);
	expect(geometry.x).toBeLessThanOrEqual(
		viewport.width - TITLE_BAR_MIN_W - origin.left - FRAME_BORDER_PX
	);
	expect(geometry.y).toBeGreaterThanOrEqual(-origin.top - FRAME_BORDER_PX);
	expect(geometry.y).toBeLessThanOrEqual(
		viewport.height - TITLE_BAR_MIN_H - origin.top - FRAME_BORDER_PX
	);
}

/** NT3 measured on the real DOM: title-bar box ∩ viewport ≥ (64 × 32). */
async function expectVisibleTitlebarArea(
	page: Page,
	kind: string,
	minW = TITLE_BAR_MIN_W,
	minH = TITLE_BAR_MIN_H
): Promise<void> {
	const box = await frameOf(page, kind).locator('.mai-window-frame__titlebar').boundingBox();
	if (!box) throw new Error(`${kind} title bar is not rendered`);
	const vp = await liveViewportOf(page);
	const visibleWidth = Math.min(box.x + box.width, vp.width) - Math.max(box.x, 0);
	const visibleHeight = Math.min(box.y + box.height, vp.height) - Math.max(box.y, 0);
	expect(visibleWidth).toBeGreaterThanOrEqual(minW);
	expect(visibleHeight).toBeGreaterThanOrEqual(minH);
}

/**
 * Identity + pinned-scope fingerprint: everything a viewport transition
 * must NOT touch (brief acceptance 2, NT5). Geometry is excluded.
 */
function identityOf(window: ProbeWindow): unknown {
	return {
		window_id: window.window_id,
		panel_kind: window.panel_kind,
		scope: window.scope,
		presentation: window.presentation,
		z: window.z,
		title_override: window.title_override
	};
}

test('NT3: a real narrowing viewport reflows the window that no longer fits to a recoverable title bar', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	const before = await snapshotOf(page);
	const notesBefore = windowOf(before, 'notes');
	const chatBefore = windowOf(before, 'chat');
	const origin = await originOf(page);

	// 1280 → 640: the notes window's title bar (starting at x = 620) crosses
	// the clamp threshold (640 − 64 − left − border ≈ 567); the chat window
	// (40 + 480 = 520) still fits with its full bar. Real browser resize
	// stream. (NT3 bounds the BAR, not the whole window — at 800 the notes
	// bar 620..684 would still be fully visible and nothing may move.)
	await page.setViewportSize({ width: 640, height: 560 });
	await expect
		.poll(() => revisionOf(page), { timeout: 5_000 })
		.toBeGreaterThan(before.revision);

	const after = await snapshotOf(page);
	const notesAfter = windowOf(after, 'notes');
	const chatAfter = windowOf(after, 'chat');
	const viewport = await liveViewportOf(page);

	// Exactly one window moved; the other is untouched (no spurious writes).
	expect(after.revision - before.revision).toBe(1);
	expect(notesAfter.geometry.x).toBe(
		viewport.width - TITLE_BAR_MIN_W - origin.left - FRAME_BORDER_PX
	);
	expect(notesAfter.geometry.x).not.toBe(notesBefore.geometry.x);
	expect(chatAfter.geometry).toEqual(chatBefore.geometry);

	// NT3 measured on the real DOM for BOTH windows.
	await expectVisibleTitlebarArea(page, 'chat');
	await expectVisibleTitlebarArea(page, 'notes');

	// The explicit pass agrees: fully in-bounds ⇒ empty report, no churn.
	const report = await page.evaluate(() => window.__maiWindowResponsiveApi!.reflow());
	expect(report.inspected).toBe(2);
	expect(report.adjustments).toEqual([]);
	expect(after.revision).toBe(before.revision + 1);
	expect(errors).toEqual([]);
});

test('no-op discipline: widening (zoom-out direction) never moves an in-bounds window or churns the revision', async ({
	page
}) => {
	const before = await snapshotOf(page);
	const chatBefore = windowOf(before, 'chat');
	const notesBefore = windowOf(before, 'notes');

	await page.setViewportSize({ width: 1600, height: 900 });
	await page.waitForTimeout(150);

	const after = await snapshotOf(page);
	expect(after.revision).toBe(before.revision);
	expect(windowOf(after, 'chat').geometry).toEqual(chatBefore.geometry);
	expect(windowOf(after, 'notes').geometry).toEqual(notesBefore.geometry);

	// The explicit pass agrees: fully in-bounds ⇒ empty report.
	const report = await page.evaluate(() => window.__maiWindowResponsiveApi!.reflow());
	expect(report).toEqual({ inspected: 2, adjustments: [] });
});

test('PT6: previously offscreen coordinates recover with pinned identity, scope, z and focus', async ({
	page
}) => {
	const origin = await originOf(page);

	// Place the chat window fully offscreen (the committed-geometry class a
	// stored layout can legitimately carry before its clamp) — committed
	// through the same validated `move` transition, integers only.
	const offscreen = await page.evaluate(() =>
		window.__maiWindowResponsiveApi!.moveWindow('chat', {
			x: 2000,
			y: 1500,
			width: 480,
			height: 320
		})
	);
	expect(offscreen).toEqual({ x: 2000, y: 1500, width: 480, height: 320 });

	const before = await snapshotOf(page);
	const chatBefore = windowOf(before, 'chat');

	await page.setViewportSize({ width: 800, height: 560 });
	await expect.poll(() => revisionOf(page)).toBeGreaterThan(before.revision);

	const after = await snapshotOf(page);
	const chatAfter = windowOf(after, 'chat');
	const viewport = await liveViewportOf(page);

	// Recovered INSIDE the viewport at the exact NT3 corner (bar inset
	// applied), identity intact.
	expect(chatAfter.geometry).toEqual({
		x: viewport.width - TITLE_BAR_MIN_W - origin.left - FRAME_BORDER_PX,
		y: viewport.height - TITLE_BAR_MIN_H - origin.top - FRAME_BORDER_PX,
		width: 480,
		height: 320
	});
	expect(identityOf(chatAfter)).toEqual(identityOf(chatBefore));
	expect(after.focused_id).toBe(chatBefore.window_id);
	await expectVisibleTitlebarArea(page, 'chat');

	// Widening back does NOT undo the recovery (no flip-flop churn).
	await page.setViewportSize({ width: 1280, height: 720 });
	await page.waitForTimeout(150);
	const widened = await snapshotOf(page);
	expect(widened.revision).toBe(after.revision);
	expect(windowOf(widened, 'chat').geometry).toEqual(chatAfter.geometry);
});

test('zoom-in via a fractional CSS-px viewport yields integer-exact geometry through the real resize event', async ({
	page
}) => {
	const origin = await originOf(page);
	const before = await snapshotOf(page);

	// Browser zoom shrinks the CSS-px viewport and can make it fractional;
	// the injected viewport seam reproduces exactly that input class, and
	// the REAL window resize event drives the REAL listener (synchronous).
	await page.evaluate(() => {
		window.__maiWindowResponsiveApi!.setViewportOverride({ width: 512.4, height: 383.6 });
		window.dispatchEvent(new Event('resize'));
	});

	const after = await snapshotOf(page);
	const chatAfter = windowOf(after, 'chat');
	const notesAfter = windowOf(after, 'notes');

	// Both windows satisfy NT3 inside the 512.4×383.6 CSS-px viewport,
	// integers only (ADR §8 rule 2 — the state layer validates, so a
	// non-integer clamp would have been rejected and observable as a
	// missing move).
	expect(after.revision).toBeGreaterThan(before.revision);
	for (const geometry of [chatAfter.geometry, notesAfter.geometry]) {
		for (const value of Object.values(geometry)) {
			expect(Number.isInteger(value)).toBe(true);
		}
		expectNt3Bounds(geometry, { width: 512, height: 384 }, origin);
	}
	await expectVisibleTitlebarArea(page, 'chat');
	await expectVisibleTitlebarArea(page, 'notes');

	// Clearing the override and re-firing is a no-op: the provider is read
	// per event, and the clamped state is stable (no churn).
	const stable = await snapshotOf(page);
	await page.evaluate(() => {
		window.__maiWindowResponsiveApi!.setViewportOverride(null);
		window.dispatchEvent(new Event('resize'));
	});
	expect((await snapshotOf(page)).revision).toBe(stable.revision);
});

test('device-metrics zoom in real Chromium clamps like zoom-in; clearing it does not churn', async ({
	page
}) => {
	const origin = await originOf(page);
	const before = await snapshotOf(page);
	const chatBefore = windowOf(before, 'chat');
	const notesBefore = windowOf(before, 'notes');

	const session = await page.context().newCDPSession(page);
	await session.send('Emulation.setDeviceMetricsOverride', {
		width: 640,
		height: 480,
		deviceScaleFactor: 2,
		mobile: false
	});
	await expect
		.poll(() => revisionOf(page), { timeout: 5_000 })
		.toBeGreaterThan(before.revision);

	const zoomed = await snapshotOf(page);
	const chatZoomed = windowOf(zoomed, 'chat');
	const notesZoomed = windowOf(zoomed, 'notes');

	// Fewer CSS px: the notes window's bar crosses the clamp threshold and is
	// pulled back; the chat window (40 + 480) still fits the ~625 CSS-px
	// width and is untouched.
	expect(notesZoomed.geometry.x).toBeLessThanOrEqual(
		640 - TITLE_BAR_MIN_W - origin.left - FRAME_BORDER_PX
	);
	expect(notesZoomed.geometry.x).not.toBe(notesBefore.geometry.x);
	expect(chatZoomed.geometry).toEqual(chatBefore.geometry);
	await expectVisibleTitlebarArea(page, 'chat');
	await expectVisibleTitlebarArea(page, 'notes');

	await session.send('Emulation.clearDeviceMetricsOverride');
	await page.waitForTimeout(150);
	const restored = await snapshotOf(page);
	// Everything re-fits the original viewport: no further moves, no churn.
	expect(restored.revision).toBe(zoomed.revision);
});

test('registry min floors win on degenerate tiny viewports — the a11y minimum is never violated', async ({
	page
}) => {
	const before = await snapshotOf(page);

	// 240×160 is below BOTH entry minimums (chat 250×180, notes global
	// default 320×200): the floor must win over the viewport cap.
	await page.setViewportSize({ width: 240, height: 160 });
	await expect.poll(() => revisionOf(page)).toBeGreaterThan(before.revision);

	const after = await snapshotOf(page);
	const origin = await originOf(page);
	const viewport = await liveViewportOf(page);

	const chatAfter = windowOf(after, 'chat');
	expect(chatAfter.geometry.width).toBe(CHAT_MIN_W);
	expect(chatAfter.geometry.height).toBe(CHAT_MIN_H);
	expectNt3Bounds(chatAfter.geometry, viewport, origin);

	const notesAfter = windowOf(after, 'notes');
	expect(notesAfter.geometry.width).toBe(DEFAULT_MIN_W);
	expect(notesAfter.geometry.height).toBe(DEFAULT_MIN_H);
	expectNt3Bounds(notesAfter.geometry, viewport, origin);

	// The state layer accepted every clamp (integers), and the real DOM
	// still shows a hit-testable title bar for both windows.
	await expectVisibleTitlebarArea(page, 'chat');
	await expectVisibleTitlebarArea(page, 'notes');
});

test('keyboard access: focused title bar and tab order survive the viewport transition', async ({
	page
}) => {
	const chatId = (await snapshotOf(page)).windows.find((w) => w.panel_kind === 'chat')!
		.window_id;

	// Tab reaches the first window's title bar (ADR §6: the title bar is
	// focusable; focusin raises the window — PT2 keyboard half).
	await page.keyboard.press('Tab');
	const focusedBefore = await page.evaluate(() => {
		const active = document.activeElement;
		return {
			isTitle:
				active instanceof HTMLElement && active.classList.contains('mai-window-frame__title'),
			windowId: active?.closest('[role="dialog"]')?.getAttribute('data-window-id')
		};
	});
	expect(focusedBefore).toEqual({ isTitle: true, windowId: chatId });

	const before = await snapshotOf(page);
	await page.setViewportSize({ width: 640, height: 560 });
	await expect.poll(() => revisionOf(page)).toBeGreaterThan(before.revision);

	// The SAME element is still focused (keyed DOM, no reparent) and the
	// state still names that window as focused; tabbing continues into the
	// window controls (no focus loss, no trap).
	const focusedAfter = await page.evaluate(() => {
		const active = document.activeElement;
		return {
			isTitle:
				active instanceof HTMLElement && active.classList.contains('mai-window-frame__title'),
			windowId: active?.closest('[role="dialog"]')?.getAttribute('data-window-id')
		};
	});
	expect(focusedAfter).toEqual(focusedBefore);
	expect((await snapshotOf(page)).focused_id).toBe(chatId);

	await page.keyboard.press('Tab');
	const afterTab = await page.evaluate(() => ({
		label: (document.activeElement as HTMLElement | null)?.getAttribute('aria-label')
	}));
	expect(afterTab.label).toBe('Minimize');
});

test('listener lifecycle: stop detaches, restart reattaches, and start is idempotent', async ({
	page
}) => {
	// Idempotent attach: with the listener already running (beforeEach), the
	// module must return the PENDING stop for both calls — one listener per
	// controller, never two.
	const starts = await page.evaluate(() => {
		const result = window.__maiWindowResponsiveApi!.startTwice();
		result.stop();
		return { identical: result.identical };
	});
	expect(starts.identical).toBe(true);

	// Detached: a viewport change moves nothing (synchronous seam proof).
	await page.evaluate(() => window.__maiWindowResponsiveApi!.stop());
	const before = await snapshotOf(page);
	await page.setViewportSize({ width: 700, height: 520 });
	await page.waitForTimeout(150);
	expect((await snapshotOf(page)).revision).toBe(before.revision);

	// Reattached: the same change class reflows again (driven through the
	// synchronous fixture seam for determinism).
	await page.evaluate(() => {
		window.__maiWindowResponsiveApi!.start();
		window.__maiWindowResponsiveApi!.setViewportOverride({ width: 500, height: 400 });
		window.dispatchEvent(new Event('resize'));
	});
	await expect.poll(() => revisionOf(page)).toBeGreaterThan(before.revision);
	const reflowed = await snapshotOf(page);
	const origin = await originOf(page);
	for (const window of reflowed.windows) {
		expectNt3Bounds(window.geometry, { width: 500, height: 400 }, origin);
	}
	expect(windowOf(reflowed, 'notes').geometry.x).toBe(
		500 - TITLE_BAR_MIN_W - origin.left - FRAME_BORDER_PX
	);
});
