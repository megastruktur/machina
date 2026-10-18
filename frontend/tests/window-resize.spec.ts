import { expect, test, type Locator, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-window-resize — real-browser proofs for the pointer resize adapter
 * (frontend/src/lib/workspace/resize.ts bound by WindowFrame's edge/corner
 * handles).
 *
 * Focused per the test policy: the adapter runs in real Chromium against the
 * reviewed 01-web component fixture (tests/fixtures/component-host.svelte) —
 * no backend, no invented API. Harness convention is the sibling specs':
 * an IN-MEMORY Vite production build of two virtual modules (a rune-free
 * fixture controller around the real `createWorkspaceState` + the real
 * `WindowFrame`, and a mount entry), served through route interception.
 *
 * Scenario coverage (brief "feature-gate coverage", runtime owner 01-verify;
 * registry row WS-PT3/NT1):
 *  - PT3: resize from every edge and corner updates shared state + DOM
 *    through the single `move` transition; fixed edges stay pinned;
 *    per-entry `min_geometry` clamps every direction (chat window: 250×180
 *    via the frame prop seam); the frozen frame default (320×200,
 *    DEFAULT_MIN_GEOMETRY) applies when no entry minimum is passed
 *  - NT3: west/north clamps keep a ≥64×32 visible, hit-testable title-bar
 *    area inside the viewport (measured on the real DOM)
 *  - NT1: pointercancel / lost capture / unmount mid-gesture revert to the
 *    committed (already clamped) geometry and recover; no stuck state
 *  - separation: a resize never drags, a drag never resizes, chrome
 *    controls and body text/scroll/selection never resize
 *  - no accumulated event handlers after repeated mount cycles; no hidden
 *    globals; no domain channel
 *
 * Known limitation (recorded in the card evidence): Chromium cannot produce
 * a real mouse `pointercancel`, and real touch cancel is prevented by the
 * adapter's `touch-action: none` (its purpose), so the cancel/lost-capture
 * cases dispatch the same events the browser would, targeted at the bound
 * handle, and assert the adapter's contract.
 */

const FIXTURE_URL = '/mai-window-resize-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const APP_ID = 'virtual:mai-window-resize-app.svelte';
const ENTRY_ID = 'virtual:mai-window-resize-entry.js';

const FRAME = `${FRONTEND_ROOT}src/lib/workspace/WindowFrame.svelte`;
const STATE = `${FRONTEND_ROOT}src/lib/workspace/state.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const RUN_ID_A = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';

/** Frozen NT3 constants (types.ts TITLE_BAR_MIN — values are contractual). */
const TITLE_BAR_MIN_W = 64;
const TITLE_BAR_MIN_H = 32;
/** The fixture's per-entry minimum for the chat window (frame prop seam). */
const CHAT_MIN_W = 250;
const CHAT_MIN_H = 180;
/** The frozen frame default (DEFAULT_MIN_GEOMETRY), applied to prefs. */
const DEFAULT_MIN_W = 320;
const DEFAULT_MIN_H = 200;

/**
 * Rune-free fixture controller (virtual .svelte modules compile in legacy
 * mode inside this in-memory production build — sibling-spec convention):
 * the thin future-workspace-runtime role. It wires the frame's geometry
 * intent (drag AND resize) to the real `move` transition — the adapters
 * themselves never touch state. The chat window passes an explicit
 * `minGeometry` (the per-entry clamp seam); prefs passes none, so the
 * frame's frozen default minimum is what its handles enforce.
 */
const APP_SOURCE = `
<script>
	import WindowFrame from '${FRAME}';
	import { createWorkspaceState } from '${STATE}';
	import ComponentHost from '${HOST}';
	import { writable } from 'svelte/store';

	const api = window.__maiWindowResizeApi;

	const state = createWorkspaceState({
		knownPanelKinds: new Set(['chat', 'model-settings', 'notes']),
		instanceKey: () => null
	});

	const chat = state.open({
		panel_kind: 'chat',
		scope: { kind: 'server', server_id: '${SERVER_A}', object: { kind: 'run', id: '${RUN_ID_A}' } },
		geometry: { x: 40, y: 40, width: 480, height: 320 }
	});
	const global = state.open({
		panel_kind: 'model-settings',
		scope: { kind: 'global' },
		geometry: { x: 620, y: 140, width: 360, height: 260 },
		title_override: 'Preferences'
	});
	if (!chat.ok || !global.ok) throw new Error('fixture opens must validate');

	api.windowIds = { chat: chat.descriptor.window_id, global: global.descriptor.window_id };
	api.snapshot = () => state.snapshot();
	api.closeChat = () => {
		state.close(chat.descriptor.window_id);
	};
	api.cycleThird = (n) => {
		for (let i = 0; i < n; i += 1) {
			const res = state.open({
				panel_kind: 'notes',
				scope: { kind: 'global' },
				geometry: { x: 20, y: 20, width: 200, height: 150 }
			});
			if (!res.ok) throw new Error('notes open failed: ' + res.issue.code);
			state.close(res.descriptor.window_id);
		}
	};

	const titles = {
		[chat.descriptor.window_id]: 'Chat',
		[global.descriptor.window_id]: 'Preferences'
	};
	const contextLabel = 'Server Alpha';

	// Stable DOM order (open order): raising must not reparent subtrees
	// (sibling-spec convention; visual stacking is descriptor.z).
	const openOrder = {};
	openOrder[chat.descriptor.window_id] = 0;
	openOrder[global.descriptor.window_id] = 1;

	function inOpenOrder(snap) {
		const windows = [...snap.windows].sort(
			(a, b) => (openOrder[a.window_id] ?? 0) - (openOrder[b.window_id] ?? 0)
		);
		return { revision: snap.revision, windows, focused_id: snap.focused_id };
	}

	const snapshotStore = writable(inOpenOrder(state.snapshot()));
	state.subscribe((next) => snapshotStore.set(inOpenOrder(next)));
</script>

<ComponentHost label="MAI window resize fixture">
	<div
		class="mai-desktop"
		data-testid="desktop"
		style="position: relative; width: 1000px; height: 560px;"
	>
		{#each $snapshotStore.windows as descriptor (descriptor.window_id)}
			<WindowFrame
				{descriptor}
				title={titles[descriptor.window_id] ?? descriptor.panel_kind}
				{contextLabel}
				focused={$snapshotStore.focused_id === descriptor.window_id}
				minGeometry={descriptor.panel_kind === 'chat'
					? { width: ${CHAT_MIN_W}, height: ${CHAT_MIN_H} }
					: undefined}
				onactivate={(id) => {
					api.activates.push(id);
					state.focus(id);
				}}
				onminimize={(id) => api.minimizes.push(id)}
				ongeometry={(id, geometry) => {
					api.resizes.push({
						id,
						x: geometry.x,
						y: geometry.y,
						width: geometry.width,
						height: geometry.height
					});
					const result = state.move(id, geometry);
					if (!result.ok) throw new Error('move rejected: ' + result.issue.code);
				}}
			>
				<div class="sample" data-sample={descriptor.panel_kind}>
					<p data-sample-text={descriptor.panel_kind}>
						{titles[descriptor.window_id] ?? descriptor.panel_kind} inert sample body
						text — selectable fixture content.
					</p>
					<button type="button" data-sample-button={descriptor.panel_kind}>
						{titles[descriptor.window_id] ?? descriptor.panel_kind} action
					</button>
					{#if descriptor.panel_kind === 'chat'}
						<div data-tall-body style="height: 600px;"></div>
					{/if}
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
	resizes: [],
	minimizes: [],
	activates: [],
	clicks: {},
	pointerId: null,
	windowIds: null,
	snapshot: null,
	closeChat: null,
	cycleThird: null
};
window.__maiWindowResizeApi = api;

// Inert sample content: count interactions by delegation so the raw fixture
// content stays event-free and tests read native click outcomes only.
document.addEventListener('click', (event) => {
	if (!(event.target instanceof Element)) return;
	const sample = event.target.closest('[data-sample-button]');
	if (sample) {
		const key = sample.getAttribute('data-sample-button') ?? '';
		api.clicks[key] = (api.clicks[key] ?? 0) + 1;
	}
});

mount(App, { target: document.getElementById('app'), props: { api } });
`;

/** Minimal bundle shape (same structural boundary as the sibling specs). */
type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

interface ProbeWindow {
	window_id: string;
	panel_kind: string;
	presentation: string;
	z: number;
	geometry: { x: number; y: number; width: number; height: number };
}

interface ProbeSnapshot {
	revision: number;
	windows: ProbeWindow[];
	focused_id: string | null;
}

interface ProbeApi {
	resizes: Array<{ id: string; x: number; y: number; width: number; height: number }>;
	minimizes: string[];
	activates: string[];
	clicks: Record<string, number>;
	pointerId: number | null;
	windowIds: { chat: string; global: string } | null;
	snapshot(): ProbeSnapshot;
	closeChat(): void;
	cycleThird(n: number): void;
}

declare global {
	interface Window {
		/** Spec-unique probe global (no collision with the sibling specs). */
		__maiWindowResizeApi?: ProbeApi;
	}
}

let harnessJs = '';
let harnessCss = '';

/** Serves the virtual modules from the in-memory bundle; nothing hits disk. */
function harnessPlugin(appSource: string, entrySource: string): Plugin {
	return {
		name: 'mai-window-resize-harness',
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
			outDir: 'node_modules/.mai-window-resize-fixture',
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
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI window resize fixture</title>
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
});

async function probe(page: Page): Promise<ProbeApi> {
	const value = await page.evaluate(() => window.__maiWindowResizeApi);
	if (!value) throw new Error('fixture probe api is missing');
	return value;
}

async function snapshotOf(page: Page): Promise<ProbeSnapshot> {
	return page.evaluate(() => window.__maiWindowResizeApi!.snapshot());
}

/** Geometry plus identity, flattened — what the resize assertions compare. */
function geometryOf(
	snapshot: ProbeSnapshot,
	kind: string
): { id: string; x: number; y: number; width: number; height: number } {
	const found = snapshot.windows.find((window) => window.panel_kind === kind);
	if (!found) throw new Error(`no ${kind} window in snapshot`);
	return { id: found.window_id, ...found.geometry };
}

/** Computed frame position/size from the real DOM. */
function posOf(frame: Locator): Promise<Record<string, string>> {
	return frame.evaluate((el) => {
		const style = getComputedStyle(el);
		return { left: style.left, top: style.top, width: style.width, height: style.height };
	});
}

const chatFrame = (page: Page) => page.locator('[role="dialog"][data-panel-kind="chat"]');
const prefsFrame = (page: Page) =>
	page.locator('[role="dialog"][data-panel-kind="model-settings"]');
const chatTitlebar = (page: Page) => chatFrame(page).locator('.mai-window-frame__titlebar');
const handleOf = (page: Page, kind: string, direction: string) =>
	page.locator(`[role="dialog"][data-panel-kind="${kind}"] [data-resize-direction="${direction}"]`);

/**
 * The title-bar viewport origin, derived EXACTLY like the resize adapter
 * does (bar rect minus the descriptor position, so the 1px border is
 * included), plus the live viewport size — the NT3 expectations must not
 * hardcode constants.
 */
async function titleBarOriginOf(page: Page, panelKind: string): Promise<{
	origin: { left: number; top: number };
	vp: { width: number; height: number };
}> {
	return page.evaluate((kind) => {
		const frame = document.querySelector(`[role="dialog"][data-panel-kind="${kind}"]`);
		const titlebar = frame?.querySelector('.mai-window-frame__titlebar');
		if (!frame || !titlebar) throw new Error('frame or title bar is missing');
		const rect = titlebar.getBoundingClientRect();
		const style = getComputedStyle(frame);
		return {
			origin: {
				left: rect.left - Number.parseInt(style.left, 10),
				top: rect.top - Number.parseInt(style.top, 10)
			},
			vp: { width: window.innerWidth, height: window.innerHeight }
		};
	}, panelKind);
}

/**
 * Grabs the window's named resize handle and resizes by (dx, dy) with
 * distinct intermediate moves. Pointer capture keeps the gesture on the
 * handle even when the pointer crosses sibling frames or leaves the
 * desktop region.
 */
async function resizeBy(
	page: Page,
	kind: string,
	direction: string,
	dx: number,
	dy: number
): Promise<void> {
	const box = await handleOf(page, kind, direction).boundingBox();
	if (!box) throw new Error(`${direction} handle of ${kind} is not rendered`);
	const startX = box.x + box.width / 2;
	const startY = box.y + box.height / 2;
	await page.mouse.move(startX, startY);
	await page.mouse.down();
	await page.mouse.move(startX + dx / 2, startY + dy / 2, { steps: 3 });
	await page.mouse.move(startX + dx, startY + dy, { steps: 3 });
	await page.mouse.up();
}

/** Starts a resize gesture (down + one move) and leaves it live. */
async function beginResize(
	page: Page,
	kind: string,
	direction: string,
	dx: number,
	dy: number
): Promise<void> {
	const box = await handleOf(page, kind, direction).boundingBox();
	if (!box) throw new Error(`${direction} handle of ${kind} is not rendered`);
	const startX = box.x + box.width / 2;
	const startY = box.y + box.height / 2;
	await page.mouse.move(startX, startY);
	await page.mouse.down();
	await page.mouse.move(startX + dx, startY + dy, { steps: 3 });
}

/**
 * Grabs the chat title bar and drags by (dx, dy) — used to position the
 * window before resize gestures (the NT3 expectations need known
 * off-center bases). Grab point is the center of the VISIBLE part of the
 * bar (after a clamp the element box can stick out of the viewport).
 */
async function dragChatTitle(page: Page, dx: number, dy: number): Promise<void> {
	const box = await chatTitlebar(page).boundingBox();
	if (!box) throw new Error('chat title bar is not rendered');
	const vp = page.viewportSize();
	if (!vp) throw new Error('page has no viewport');
	const left = Math.max(box.x, 0);
	const top = Math.max(box.y, 0);
	const right = Math.min(box.x + box.width, vp.width);
	const bottom = Math.min(box.y + box.height, vp.height);
	if (right - left < 8 || bottom - top < 8) throw new Error('title bar has no visible area');
	const startX = left + (right - left) / 2;
	const startY = top + (bottom - top) / 2;
	await page.mouse.move(startX, startY);
	await page.mouse.down();
	await page.mouse.move(startX + dx / 2, startY + dy / 2, { steps: 3 });
	await page.mouse.move(startX + dx, startY + dy, { steps: 3 });
	await page.mouse.up();
}

/** NT3 measured on the real DOM: title-bar box ∩ viewport ≥ (minW × minH). */
async function expectVisibleTitlebarArea(
	page: Page,
	kind: string,
	minW: number,
	minH: number
): Promise<void> {
	const box = await chatFrame(page).locator('.mai-window-frame__titlebar').boundingBox();
	if (!box) throw new Error(`${kind} title bar is not rendered`);
	const vp = page.viewportSize();
	if (!vp) throw new Error('page has no viewport');
	const visibleWidth = Math.min(box.x + box.width, vp.width) - Math.max(box.x, 0);
	const visibleHeight = Math.min(box.y + box.height, vp.height) - Math.max(box.y, 0);
	expect(visibleWidth).toBeGreaterThanOrEqual(minW);
	expect(visibleHeight).toBeGreaterThanOrEqual(minH);
}

test('PT3: edge resizes update shared state and the DOM through the single move transition', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);
	await expect(page.getByTestId('component-host')).toHaveAttribute('data-mounted', 'true');

	const chat = chatFrame(page);
	const prefs = prefsFrame(page);
	const before = geometryOf(await snapshotOf(page), 'chat');
	expect(before).toMatchObject({ x: 40, y: 40, width: 480, height: 320 });
	const beforeRevision = (await snapshotOf(page)).revision;
	const beforeResizes = (await probe(page)).resizes.length;

	// East edge: width grows, position and height untouched.
	await resizeBy(page, 'chat', 'e', 120, 0);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after).toEqual({ id: before.id, x: 40, y: 40, width: 600, height: 320 });
		expect(await posOf(chat)).toEqual({
			left: '40px',
			top: '40px',
			width: '600px',
			height: '320px'
		});
		expect((await snapshotOf(page)).revision).toBeGreaterThan(beforeRevision);
	}

	// South edge: height grows, position and width untouched.
	await resizeBy(page, 'chat', 's', 0, 80);
	expect(geometryOf(await snapshotOf(page), 'chat')).toEqual({
		id: before.id,
		x: 40,
		y: 40,
		width: 600,
		height: 400
	});

	// West edge: the RIGHT edge stays pinned — x absorbs the delta and the
	// width shrinks by the same amount (a resize must never drag).
	await resizeBy(page, 'chat', 'w', 60, 0);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after).toEqual({ id: before.id, x: 100, y: 40, width: 540, height: 400 });
		expect(after.x + after.width).toBe(640); // pinned east edge
	}

	// North edge: the BOTTOM edge stays pinned.
	await resizeBy(page, 'chat', 'n', 0, 40);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after).toEqual({ id: before.id, x: 100, y: 80, width: 540, height: 360 });
		expect(after.y + after.height).toBe(440); // pinned south edge
		expect(await posOf(chat)).toEqual({
			left: '100px',
			top: '80px',
			width: '540px',
			height: '360px'
		});
	}

	// Every emission went through the controller for the resized window only.
	const recent = (await probe(page)).resizes.slice(beforeResizes);
	expect(recent.length).toBeGreaterThan(0);
	expect(recent.every((resize) => resize.id === before.id)).toBe(true);
	expect(recent.at(-1)).toMatchObject({ x: 100, y: 80, width: 540, height: 360 });

	// The other window is untouched in state and DOM.
	expect(geometryOf(await snapshotOf(page), 'model-settings')).toMatchObject({
		x: 620,
		y: 140,
		width: 360,
		height: 260
	});
	expect(await posOf(prefs)).toEqual({
		left: '620px',
		top: '140px',
		width: '360px',
		height: '260px'
	});
	expect(errors).toEqual([]);
});

test('PT3: corner resizes move both axes; nw keeps the bottom-right corner pinned', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const before = geometryOf(await snapshotOf(page), 'chat');

	// Southeast: width and height grow, position untouched.
	await resizeBy(page, 'chat', 'se', 60, 40);
	expect(geometryOf(await snapshotOf(page), 'chat')).toEqual({
		id: before.id,
		x: 40,
		y: 40,
		width: 540,
		height: 360
	});

	// Northwest: x/y absorb the delta; right/bottom stay pinned (partially
	// offscreen geometry is legal — NT3 guards the title-bar strip only).
	await resizeBy(page, 'chat', 'nw', -50, -30);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after).toEqual({ id: before.id, x: -10, y: 10, width: 590, height: 390 });
		expect(after.x + after.width).toBe(580);
		expect(after.y + after.height).toBe(400);
	}
});

test('PT3/NT3: the per-entry minimum clamps every edge and corner', async ({ page }) => {
	// East and south stop at the 250×180 entry minimum.
	await page.goto(FIXTURE_URL);
	const before = geometryOf(await snapshotOf(page), 'chat');
	await resizeBy(page, 'chat', 'e', -2000, 0);
	expect(geometryOf(await snapshotOf(page), 'chat')).toEqual({
		id: before.id,
		x: 40,
		y: 40,
		width: CHAT_MIN_W,
		height: 320
	});
	await resizeBy(page, 'chat', 's', 0, -2000);
	expect(geometryOf(await snapshotOf(page), 'chat')).toEqual({
		id: before.id,
		x: 40,
		y: 40,
		width: CHAT_MIN_W,
		height: CHAT_MIN_H
	});

	// West and north from a fresh fixture: the pinned edge minus the minimum
	// caps the moving edge (right/bottom never move).
	await page.goto(FIXTURE_URL);
	const fresh = geometryOf(await snapshotOf(page), 'chat');
	await resizeBy(page, 'chat', 'w', 2000, 0);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after).toEqual({
			id: fresh.id,
			x: 40 + 480 - CHAT_MIN_W,
			y: 40,
			width: CHAT_MIN_W,
			height: 320
		});
	}
	await resizeBy(page, 'chat', 'n', 0, 2000);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after).toEqual({
			id: fresh.id,
			x: 40 + 480 - CHAT_MIN_W,
			y: 40 + 320 - CHAT_MIN_H,
			width: CHAT_MIN_W,
			height: CHAT_MIN_H
		});
		expect(after.y + after.height).toBe(360);
	}

	// Corners clamp to the minimum in both axes at once (fresh mount per
	// phase: window ids are per-mount, never compared across reloads).
	await page.goto(FIXTURE_URL);
	const seBase = geometryOf(await snapshotOf(page), 'chat');
	await resizeBy(page, 'chat', 'se', -2000, -2000);
	expect(geometryOf(await snapshotOf(page), 'chat')).toEqual({
		id: seBase.id,
		x: 40,
		y: 40,
		width: CHAT_MIN_W,
		height: CHAT_MIN_H
	});
	await page.goto(FIXTURE_URL);
	const nwBase = geometryOf(await snapshotOf(page), 'chat');
	await resizeBy(page, 'chat', 'nw', 2000, 2000);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after).toEqual({
			id: nwBase.id,
			x: 40 + 480 - CHAT_MIN_W,
			y: 40 + 320 - CHAT_MIN_H,
			width: CHAT_MIN_W,
			height: CHAT_MIN_H
		});
	}
});

test('PT3: the frozen frame default minimum applies without an entry minimum', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	// Prefs opened without a minGeometry prop: the frame's frozen
	// DEFAULT_MIN_GEOMETRY (320×200) is the effective clamp.
	const before = geometryOf(await snapshotOf(page), 'model-settings');
	expect(before).toMatchObject({ width: 360, height: 260 });

	await resizeBy(page, 'model-settings', 'e', -2000, 0);
	expect(geometryOf(await snapshotOf(page), 'model-settings')).toEqual({
		id: before.id,
		x: 620,
		y: 140,
		width: DEFAULT_MIN_W,
		height: 260
	});
	await resizeBy(page, 'model-settings', 's', 0, -2000);
	{
		const after = geometryOf(await snapshotOf(page), 'model-settings');
		expect(after).toEqual({
			id: before.id,
			x: 620,
			y: 140,
			width: DEFAULT_MIN_W,
			height: DEFAULT_MIN_H
		});
		expect(await posOf(prefsFrame(page))).toEqual({
			left: '620px',
			top: '140px',
			width: `${DEFAULT_MIN_W}px`,
			height: `${DEFAULT_MIN_H}px`
		});
	}
});

test('NT3: west and north resize clamps keep a 64×32 visible title-bar area', async ({
	page
}) => {
	// West: drag the window near the right viewport edge first, then push
	// its left edge right past everything — the NT3 cap must bind before
	// the title bar loses 64px of visible width.
	await page.goto(FIXTURE_URL);
	await dragChatTitle(page, 1000, 80);
	const dragged = geometryOf(await snapshotOf(page), 'chat');
	expect(dragged.x).toBe(1040); // inside the drag clamp, right edge offscreen
	await resizeBy(page, 'chat', 'w', 2000, 0);
	{
		const { origin, vp } = await titleBarOriginOf(page, 'chat');
		const after = geometryOf(await snapshotOf(page), 'chat');
		const expectedX = Math.floor(vp.width - TITLE_BAR_MIN_W - origin.left);
		expect(after.x).toBe(expectedX);
		expect(after.x + after.width).toBe(1520); // pinned east edge
		expect(after.width).toBeGreaterThan(CHAT_MIN_W); // NT3 binds, not min
	}
	await expectVisibleTitlebarArea(page, 'chat', TITLE_BAR_MIN_W, TITLE_BAR_MIN_H);

	// North: drag near the bottom, then push the top edge down — the cap
	// keeps 32px of visible title-bar height.
	await page.goto(FIXTURE_URL);
	await dragChatTitle(page, 300, 520);
	const draggedDown = geometryOf(await snapshotOf(page), 'chat');
	expect(draggedDown.y).toBe(560);
	await resizeBy(page, 'chat', 'n', 0, 2000);
	{
		const { origin, vp } = await titleBarOriginOf(page, 'chat');
		const after = geometryOf(await snapshotOf(page), 'chat');
		const expectedY = Math.floor(vp.height - TITLE_BAR_MIN_H - origin.top);
		expect(after.y).toBe(expectedY);
		expect(after.y + after.height).toBe(880); // pinned south edge
	}
	await expectVisibleTitlebarArea(page, 'chat', TITLE_BAR_MIN_W, TITLE_BAR_MIN_H);
});

test('NT1: pointercancel mid-resize reverts to the committed geometry and recovers', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const before = geometryOf(await snapshotOf(page), 'chat');
	const beforeResizes = (await probe(page)).resizes.length;

	// Learn the real pointer id of the mouse gesture (browser-defined).
	await page.evaluate(() => {
		const handle = window.__maiWindowResizeApi!;
		document.addEventListener(
			'pointerdown',
			(event) => {
				handle.pointerId = event.pointerId;
			},
			{ capture: true, once: true }
		);
	});

	await beginResize(page, 'chat', 'e', 90, 0);
	const moved = geometryOf(await snapshotOf(page), 'chat');
	expect(moved.width).toBe(before.width + 90);

	// Cancel exactly the live gesture, as the browser would (e.g. touch
	// takeover — prevented on purpose here — or device rotation).
	const canceledId = (await probe(page)).pointerId;
	if (canceledId === null) throw new Error('pointer id of the gesture was not captured');
	await page.evaluate(
		({ kind, direction, pointerId }) => {
			document
				.querySelector(
					`[role="dialog"][data-panel-kind="${kind}"] [data-resize-direction="${direction}"]`
				)!
				.dispatchEvent(
					new PointerEvent('pointercancel', {
						pointerId,
						bubbles: true,
						cancelable: true,
						isPrimary: true
					})
				);
		},
		{ kind: 'chat', direction: 'e', pointerId: canceledId }
	);

	const reverted = geometryOf(await snapshotOf(page), 'chat');
	expect(reverted).toEqual({
		id: before.id,
		x: before.x,
		y: before.y,
		width: before.width,
		height: before.height
	});
	expect(await posOf(chatFrame(page))).toEqual({
		left: `${before.x}px`,
		top: `${before.y}px`,
		width: `${before.width}px`,
		height: `${before.height}px`
	});
	// The revert went through the controller exactly once.
	expect((await probe(page)).resizes.slice(beforeResizes).at(-1)).toMatchObject({
		x: before.x,
		y: before.y,
		width: before.width,
		height: before.height
	});

	// No stuck gesture state: a normal resize works right after.
	await resizeBy(page, 'chat', 'e', 50, 0);
	expect(geometryOf(await snapshotOf(page), 'chat')).toMatchObject({
		width: before.width + 50
	});
});

test('NT1: losing pointer capture mid-resize reverts and leaves no stuck state', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const before = geometryOf(await snapshotOf(page), 'chat');

	await page.evaluate(() => {
		const handle = window.__maiWindowResizeApi!;
		document.addEventListener(
			'pointerdown',
			(event) => {
				handle.pointerId = event.pointerId;
			},
			{ capture: true, once: true }
		);
	});

	await beginResize(page, 'chat', 'e', 60, 0);

	// Capture lost without up/cancel — the frame reparents under a raise, or
	// the element is removed. Contract: treat as an interruption, revert.
	const lostId = (await probe(page)).pointerId;
	if (lostId === null) throw new Error('pointer id of the gesture was not captured');
	await page.evaluate(
		({ kind, direction, pointerId }) => {
			document
				.querySelector(
					`[role="dialog"][data-panel-kind="${kind}"] [data-resize-direction="${direction}"]`
				)!
				.dispatchEvent(
					new PointerEvent('lostpointercapture', { pointerId, bubbles: true, isPrimary: true })
				);
		},
		{ kind: 'chat', direction: 'e', pointerId: lostId }
	);

	expect(geometryOf(await snapshotOf(page), 'chat')).toMatchObject({
		x: before.x,
		y: before.y,
		width: before.width,
		height: before.height
	});

	// The suppressed gesture must not resurrect on later moves.
	await page.mouse.move((await handleOf(page, 'chat', 'e').boundingBox())!.x + 160, 300, {
		steps: 2
	});
	await page.mouse.up();
	expect(geometryOf(await snapshotOf(page), 'chat')).toMatchObject({
		x: before.x,
		width: before.width
	});
});

test('NT1: unmount mid-resize cleans up — no errors, no ghost emissions, workspace healthy', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);
	const chatId = (await probe(page)).windowIds!.chat;
	const beforeResizes = (await probe(page)).resizes.length;

	await beginResize(page, 'chat', 'e', 40, 0);

	// The controller closes the window mid-gesture: the frame unmounts, the
	// action's destroy runs with a live gesture — and must stay silent.
	const resizesAtClose = (await probe(page)).resizes.length;
	await page.evaluate(() => window.__maiWindowResizeApi!.closeChat());
	await expect(page.getByRole('dialog')).toHaveCount(1);

	await page.mouse.move(700, 400, { steps: 3 });
	await page.mouse.up();

	expect((await probe(page)).resizes.length).toBe(resizesAtClose);
	expect((await probe(page)).resizes.slice(beforeResizes).every((r) => r.id === chatId)).toBe(
		true
	);
	const snapshot = await snapshotOf(page);
	expect(snapshot.windows.map((window) => window.panel_kind)).toEqual(['model-settings']);
	expect(errors).toEqual([]);

	// The surviving window resizes normally — no leaked listeners or capture.
	await resizeBy(page, 'model-settings', 'e', 40, 0);
	expect(geometryOf(await snapshotOf(page), 'model-settings')).toMatchObject({ width: 400 });
});

test('PT3: capture keeps resize tracking across sibling windows and releases outside the frame', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const before = geometryOf(await snapshotOf(page), 'chat');
	const prefsBefore = geometryOf(await snapshotOf(page), 'model-settings');
	const prefsPos = await posOf(prefsFrame(page));

	// West shrink of +800 releases INSIDE the prefs window (handle center
	// ≈ x 44 → release ≈ x 844): capture must keep the gesture on the chat
	// handle and never touch prefs.
	await resizeBy(page, 'chat', 'w', 800, 0);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		// The per-entry minimum binds (270 < NT3 cap ≈ 1175).
		expect(after).toEqual({
			id: before.id,
			x: 40 + 480 - CHAT_MIN_W,
			y: 40,
			width: CHAT_MIN_W,
			height: 320
		});
	}
	expect(geometryOf(await snapshotOf(page), 'model-settings')).toEqual(prefsBefore);
	expect(await posOf(prefsFrame(page))).toEqual(prefsPos);

	// The release click completed on the chat handle: chat raised, prefs not.
	expect((await probe(page)).activates).toEqual([before.id]);
	expect(await chatFrame(page)).toHaveAttribute('data-focused', 'true');
	expect(await prefsFrame(page)).toHaveAttribute('data-focused', 'false');
});

test('resize stays separate from drag, chrome controls and body interactions', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);
	const chat = chatFrame(page);
	const before = geometryOf(await snapshotOf(page), 'chat');
	const beforeResizes = (await probe(page)).resizes.length;

	// a) An edge resize changes size, never position — the drag adapter is
	// not involved (a drag would move x/y).
	await resizeBy(page, 'chat', 'e', 120, 0);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after).toMatchObject({ x: before.x, y: before.y, height: before.height });
	}

	// b) A title-bar drag changes position, never size — the resize adapter
	// is not involved. The drag adapter emits through the SAME frame
	// geometry channel, so emissions exist here by design; size stays.
	await dragChatTitle(page, 33, 21);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after).toMatchObject({ x: before.x + 33, y: before.y + 21 });
		expect(after.width).toBe(before.width + 120);
		expect(after.height).toBe(before.height);
	}
	const emissionsAfterDrag = (await probe(page)).resizes.length;

	// c) A chrome control click is a control interaction: intent fires, no
	// geometry emission, no size change.
	await chat.getByRole('button', { name: 'Minimize' }).click();
	expect((await probe(page)).minimizes).toEqual([before.id]);

	// d) Body text selects natively, body button clicks, body scroll — none
	// of them resize (the handles only cover the frame edges) and none of
	// them emit geometry through either adapter.
	const paragraph = chat.locator('[data-sample-text="chat"]');
	await paragraph.click({ clickCount: 3 });
	expect(await page.evaluate(() => window.getSelection()?.toString() ?? '')).toContain(
		'inert sample body text'
	);
	await chat.locator('[data-sample-button="chat"]').click();
	expect((await probe(page)).clicks.chat).toBe(1);
	const scrollTop = await chat
		.locator('.mai-window-frame__body')
		.evaluate((el) => {
			el.scrollTop = 120;
			return el.scrollTop;
		});
	expect(scrollTop).toBe(120);
	{
		const after = geometryOf(await snapshotOf(page), 'chat');
		expect(after.width).toBe(before.width + 120);
		expect(after.height).toBe(before.height);
	}

	// Controls and body interactions emitted zero geometry (the count is
	// frozen since the drag in (b)).
	expect((await probe(page)).resizes).toHaveLength(emissionsAfterDrag);

	// e) After all of that, resizing still works (controls recovered).
	await resizeBy(page, 'chat', 'e', 30, 0);
	expect(geometryOf(await snapshotOf(page), 'chat')).toMatchObject({
		width: before.width + 150
	});
	expect(errors).toEqual([]);
});

test('repeated mount cycles do not accumulate resize handlers; no hidden globals; no domain channel', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);
	const api = await probe(page);
	const chatId = api.windowIds!.chat;
	const before = geometryOf(await snapshotOf(page), 'chat');
	const beforeResizes = api.resizes.length;

	// 25 open+close cycles mount and destroy resize actions; a duplicated
	// listener would emit twice per move below.
	await page.evaluate(() => window.__maiWindowResizeApi!.cycleThird(25));

	// Focus chat first so the release click is an idempotent no-op and the
	// revision counts exactly the one resize emission.
	await chatFrame(page).locator('h2').click();
	await expect(chatFrame(page)).toHaveAttribute('data-focused', 'true');
	const revisionAfterFocus = (await snapshotOf(page)).revision;

	const box = (await handleOf(page, 'chat', 'e').boundingBox())!;
	await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
	await page.mouse.down();
	await page.mouse.move(box.x + box.width / 2 + 37, box.y + box.height / 2);
	await page.mouse.up();

	const newResizes = (await probe(page)).resizes.slice(beforeResizes);
	expect(newResizes).toHaveLength(1);
	expect(newResizes[0]).toMatchObject({
		id: chatId,
		x: before.x,
		y: before.y,
		width: before.width + 37,
		height: before.height
	});
	expect((await snapshotOf(page)).revision).toBe(revisionAfterFocus + 1);
	expect(geometryOf(await snapshotOf(page), 'chat')).toMatchObject({
		width: before.width + 37
	});

	// No module-level globals beyond the spec-unique probe handle.
	const extraGlobals = await page.evaluate(() =>
		Object.keys(window).filter(
			(key) => key.startsWith('__mai') && key !== '__maiWindowResizeApi'
		)
	);
	expect(extraGlobals).toEqual([]);

	// No domain channel: the fixture route served every resource.
	const resources: string[] = await page.evaluate(() =>
		performance.getEntriesByType('resource').map((entry) => new URL(entry.name).pathname)
	);
	expect(resources.length).toBeGreaterThan(0);
	expect(resources.every((pathname) => pathname.startsWith(FIXTURE_URL))).toBe(true);
	expect(errors).toEqual([]);
});
