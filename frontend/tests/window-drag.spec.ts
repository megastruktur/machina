import { expect, test, type Locator, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-window-drag — real-browser proofs for the pointer drag adapter
 * (frontend/src/lib/workspace/drag.ts bound by WindowFrame's title bar).
 *
 * Focused per the test policy: the adapter runs in real Chromium against the
 * reviewed 01-web component fixture (tests/fixtures/component-host.svelte) —
 * no backend, no invented API. Harness convention is the sibling specs':
 * an IN-MEMORY Vite production build of two virtual modules (a rune-free
 * fixture controller around the real `createWorkspaceState` + the real
 * `WindowFrame`, and a mount entry), served through route interception.
 *
 * Scenario coverage (brief "feature-gate coverage", runtime owner 01-verify):
 *  - PT1: drag from title-bar origins moves shared state + DOM; drag
 *    raises/focuses the window on release; release away from the original
 *    frame keeps tracking (pointer capture, no document listeners)
 *  - NT3: clamps at every viewport edge keep a ≥64×32 visible title-bar area
 *  - NT1: pointercancel / lost capture / unmount mid-gesture revert to the
 *    committed geometry and recover (a later drag works; no stuck state)
 *  - NT2: chrome controls, body text/scroll/selection never initiate a move
 *  - no accumulated event handlers after repeated mount cycles; no hidden
 *    globals; no domain channel
 *
 * Known limitation (recorded in the card evidence): Chromium cannot produce
 * a real mouse `pointercancel`, and real touch cancel is prevented by the
 * adapter's `touch-action: none` (its purpose), so the cancel/lost-capture
 * cases dispatch the same events the browser would, targeted at the bound
 * title bar, and assert the adapter's contract.
 */

const FIXTURE_URL = '/mai-window-drag-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const APP_ID = 'virtual:mai-window-drag-app.svelte';
const ENTRY_ID = 'virtual:mai-window-drag-entry.js';

const FRAME = `${FRONTEND_ROOT}src/lib/workspace/WindowFrame.svelte`;
const STATE = `${FRONTEND_ROOT}src/lib/workspace/state.ts`;
const DRAG = `${FRONTEND_ROOT}src/lib/workspace/drag.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const RUN_ID_A = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';

/**
 * Rune-free fixture controller (virtual .svelte modules compile in legacy
 * mode inside this in-memory production build — sibling-spec convention):
 * the thin future-workspace-runtime role. It wires the frame's drag intent
 * to the real `move` transition — the adapter itself never touches state.
 */
const APP_SOURCE = `
<script>
	import WindowFrame from '${FRAME}';
	import { createWorkspaceState } from '${STATE}';
	import ComponentHost from '${HOST}';
	import { writable } from 'svelte/store';

	const api = window.__maiWindowDragApi;

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

<ComponentHost label="MAI window drag fixture">
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
				onactivate={(id) => {
					api.activates.push(id);
					state.focus(id);
				}}
				onminimize={(id) => api.minimizes.push(id)}
				ongeometry={(id, geometry) => {
					api.moves.push({ id, x: geometry.x, y: geometry.y });
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
	moves: [],
	activates: [],
	minimizes: [],
	clicks: {},
	pointerId: null,
	windowIds: null,
	snapshot: null,
	closeChat: null,
	cycleThird: null
};
window.__maiWindowDragApi = api;

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
	moves: Array<{ id: string; x: number; y: number }>;
	activates: string[];
	minimizes: string[];
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
		__maiWindowDragApi?: ProbeApi;
	}
}

let harnessJs = '';
let harnessCss = '';

/** Serves the virtual modules from the in-memory bundle; nothing hits disk. */
function harnessPlugin(appSource: string, entrySource: string): Plugin {
	return {
		name: 'mai-window-drag-harness',
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
			outDir: 'node_modules/.mai-window-drag-fixture',
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
<title>MAI window drag fixture</title>
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
	const value = await page.evaluate(() => window.__maiWindowDragApi);
	if (!value) throw new Error('fixture probe api is missing');
	return value;
}

async function snapshotOf(page: Page): Promise<ProbeSnapshot> {
	return page.evaluate(() => window.__maiWindowDragApi!.snapshot());
}

/** Geometry plus identity, flattened — what the drag assertions compare. */
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

/**
 * The drag coordinate origin, derived EXACTLY like the adapter does
 * (title-bar rect minus the descriptor position, so the 1px border is
 * included), plus the live viewport size — the NT3 expectations must not
 * hardcode constants.
 */
async function dragOriginOf(page: Page, panelKind: string): Promise<{
	origin: { left: number; top: number; barWidth: number };
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
				top: rect.top - Number.parseInt(style.top, 10),
				barWidth: rect.width
			},
			vp: { width: window.innerWidth, height: window.innerHeight }
		};
	}, panelKind);
}

/**
 * Grabs the chat title bar and drags by (dx, dy) with distinct intermediate
 * moves. The grab point is the CENTER OF THE VISIBLE part of the title bar
 * (after a clamp, the element box can stick out of the viewport, and the
 * first pointerdown must land on the element), leaving the button wherever
 * the last point lands (the window follows the pointer — capture keeps
 * tracking).
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
async function expectVisibleTitlebarArea(page: Page, minW: number, minH: number): Promise<void> {
	const box = await chatTitlebar(page).boundingBox();
	if (!box) throw new Error('chat title bar is not rendered');
	const vp = page.viewportSize();
	if (!vp) throw new Error('page has no viewport');
	const visibleWidth = Math.min(box.x + box.width, vp.width) - Math.max(box.x, 0);
	const visibleHeight = Math.min(box.y + box.height, vp.height) - Math.max(box.y, 0);
	expect(visibleWidth).toBeGreaterThanOrEqual(minW);
	expect(visibleHeight).toBeGreaterThanOrEqual(minH);
}

test('PT1: dragging the title bar updates shared state, the DOM and raises the window on release', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);
	await expect(page.getByTestId('component-host')).toHaveAttribute('data-mounted', 'true');

	const chat = chatFrame(page);
	const prefs = prefsFrame(page);
	const before = geometryOf(await snapshotOf(page), 'chat');
	const beforeRevision = (await snapshotOf(page)).revision;
	const beforeMoves = (await probe(page)).moves.length;

	// The prefs window was opened last, so chat starts unfocused.
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await expect(chat).toHaveAttribute('data-focused', 'false');

	// Drag from the title TEXT (the h2 heading — a title-bar origin that is
	// not an interactive control): first by an unclamped delta, then across
	// the prefs window, releasing while hovering it — capture must keep the
	// gesture on the chat title bar and never touch prefs.
	const heading = chat.locator('h2');
	const headBox = await heading.boundingBox();
	if (!headBox) throw new Error('chat heading is not rendered');
	const grab = { x: headBox.x + headBox.width / 2, y: headBox.y + headBox.height / 2 };
	const prefsBox = await prefs.boundingBox();
	if (!prefsBox) throw new Error('prefs frame is not rendered');
	const release = { x: prefsBox.x + 40, y: prefsBox.y + 20 };
	await page.mouse.move(grab.x, grab.y);
	await page.mouse.down();
	await page.mouse.move(grab.x + 120, grab.y + 60, { steps: 6 });
	await page.mouse.move(release.x, release.y, { steps: 4 });
	await page.mouse.up();

	const after = geometryOf(await snapshotOf(page), 'chat');
	expect(after.id).toBe(before.id);
	expect({ x: after.x, y: after.y }).toEqual({
		x: Math.round(before.x + (release.x - grab.x)),
		y: Math.round(before.y + (release.y - grab.y))
	});
	expect(after.width).toBe(before.width);
	expect(after.height).toBe(before.height);

	// Shared state is the single source: the DOM renders exactly the moved
	// descriptor, and every emission carried the moved geometry.
	const domPos = await posOf(chat);
	expect(domPos).toEqual({
		left: `${after.x}px`,
		top: `${after.y}px`,
		width: `${after.width}px`,
		height: `${after.height}px`
	});
	const recentMoves = (await probe(page)).moves.slice(beforeMoves);
	expect(recentMoves.length).toBeGreaterThan(0);
	expect(recentMoves.every((move) => move.id === before.id)).toBe(true);
	expect(recentMoves[recentMoves.length - 1]).toMatchObject({ x: after.x, y: after.y });
	expect((await snapshotOf(page)).revision).toBeGreaterThan(beforeRevision);

	// Prefs is untouched: geometry, identity and its own descriptors.
	const prefsSnapshot = geometryOf(await snapshotOf(page), 'model-settings');
	expect(
	{ x: prefsSnapshot.x, y: prefsSnapshot.y, width: prefsSnapshot.width, height: prefsSnapshot.height }
).toEqual({ x: 620, y: 140, width: 360, height: 260 });
	expect(await posOf(prefs)).toEqual({
		left: '620px',
		top: '140px',
		width: '360px',
		height: '260px'
	});

	// PT1: dragging an unfocused window raises/focuses it — via the same
	// click-completion activation path as a plain click (no mid-gesture
	// raise, which would reparent the frame under the captured pointer).
	await expect(chat).toHaveAttribute('data-focused', 'true');
	await expect(prefs).toHaveAttribute('data-focused', 'false');
	expect(await chat.evaluate((el) => getComputedStyle(el).zIndex)).toBe('2');
	expect((await probe(page)).activates).toEqual([before.id]);

	expect(errors).toEqual([]);
});

test('PT1/NT3: drags clamp at every viewport edge, keeping a 64×32 visible title-bar area', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	// Far past the bottom-right corner: the frame must stop with 64px of
	// title-bar width and 32px of height still inside the viewport.
	await dragChatTitle(page, 3000, 3000);
	{
		const { origin, vp } = await dragOriginOf(page, 'chat');
		const clamped = geometryOf(await snapshotOf(page), 'chat');
		expect(clamped.x).toBe(vp.width - 64 - origin.left);
		expect(clamped.y).toBe(vp.height - 32 - origin.top);
	}
	await expectVisibleTitlebarArea(page, 64, 32);

	// Far past the top-left corner: x stops so 64px of the bar's own width
	// stays visible (the bar is the descriptor width minus both borders); y
	// stops at the viewport top edge (no negative top).
	await dragChatTitle(page, -2000, -2000);
	{
		const { origin, vp } = await dragOriginOf(page, 'chat');
		const clamped = geometryOf(await snapshotOf(page), 'chat');
		expect(clamped.x).toBe(Math.round(64 - origin.barWidth - origin.left));
		expect(clamped.y).toBe(0 - origin.top);
	}
	await expectVisibleTitlebarArea(page, 64, 32);

	// The other window is unaffected by any of it.
	const prefsSnapshot = geometryOf(await snapshotOf(page), 'model-settings');
	expect(
	{ x: prefsSnapshot.x, y: prefsSnapshot.y, width: prefsSnapshot.width, height: prefsSnapshot.height }
).toEqual({ x: 620, y: 140, width: 360, height: 260 });
});

test('PT1: capture keeps tracking through moves that leave the frame and the desktop region', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const before = geometryOf(await snapshotOf(page), 'chat');

	const box = await chatTitlebar(page).boundingBox();
	if (!box) throw new Error('chat title bar is not rendered');
	const startX = box.x + 20; // title-bar padding origin (header itself)
	const startY = box.y + box.height / 2;
	await page.mouse.move(startX, startY);
	await page.mouse.down();
	// Cross the prefs window mid-gesture…
	const prefsBox = await prefsFrame(page).boundingBox();
	if (!prefsBox) throw new Error('prefs frame is not rendered');
	await page.mouse.move(prefsBox.x + prefsBox.width / 2, prefsBox.y + prefsBox.height / 2, {
		steps: 5
	});
	// …then leave the desktop region entirely, and release OUTSIDE the moved
	// frame's original area, below it (still inside the viewport).
	const release = { x: 50, y: 660 };
	await page.mouse.move(release.x, release.y, { steps: 5 });
	await page.mouse.up();

	const after = geometryOf(await snapshotOf(page), 'chat');
	expect({ x: after.x, y: after.y }).toEqual({
		x: Math.round(before.x + (release.x - startX)),
		y: Math.round(before.y + (release.y - startY))
	});
	// The release point was over empty page — no foreign activation happened;
	// the drag itself activated only the dragged window (its own click).
	const allMoves = (await probe(page)).moves;
	const moved = allMoves.filter((move) => move.id === before.id);
	expect(moved.length).toBeGreaterThan(0);
	expect(allMoves.filter((move) => move.id !== before.id)).toEqual([]);
	const prefsSnapshot = geometryOf(await snapshotOf(page), 'model-settings');
	expect(
	{ x: prefsSnapshot.x, y: prefsSnapshot.y, width: prefsSnapshot.width, height: prefsSnapshot.height }
).toEqual({ x: 620, y: 140, width: 360, height: 260 });
});

test('NT1: pointercancel mid-gesture reverts to the committed geometry and recovers', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const before = geometryOf(await snapshotOf(page), 'chat');
	const beforeMoves = (await probe(page)).moves.length;

	// Learn the real pointer id of the mouse gesture (browser-defined).
	await page.evaluate(() => {
		const handle = window.__maiWindowDragApi!;
		document.addEventListener(
			'pointerdown',
			(event) => {
				handle.pointerId = event.pointerId;
			},
			{ capture: true, once: true }
		);
	});

	const box = await chatTitlebar(page).boundingBox();
	if (!box) throw new Error('chat title bar is not rendered');
	await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
	await page.mouse.down();
	await page.mouse.move(box.x + box.width / 2 + 90, box.y + box.height / 2 + 50, { steps: 3 });
	const movedGeometry = geometryOf(await snapshotOf(page), 'chat');
	expect({ x: movedGeometry.x, y: movedGeometry.y }).toEqual({ x: before.x + 90, y: before.y + 50 });

	// Cancel exactly the live gesture, as the browser would (e.g. touch
	// takeover — prevented on purpose here — or device rotation).
	const canceledId = (await probe(page)).pointerId;
	if (canceledId === null) throw new Error('pointer id of the gesture was not captured');
	await page.evaluate((pointerId) => {
		document
			.querySelector('[role="dialog"][data-panel-kind="chat"] .mai-window-frame__titlebar')!
			.dispatchEvent(
				new PointerEvent('pointercancel', {
					pointerId,
					bubbles: true,
					cancelable: true,
					isPrimary: true
				})
			);
	}, canceledId);

	const reverted = geometryOf(await snapshotOf(page), 'chat');
	expect({ x: reverted.x, y: reverted.y }).toEqual({ x: before.x, y: before.y });
	expect(await posOf(chatFrame(page))).toEqual({
		left: `${before.x}px`,
		top: `${before.y}px`,
		width: `${before.width}px`,
		height: `${before.height}px`
	});
	// The revert went through the controller exactly once.
	expect((await probe(page)).moves.slice(beforeMoves).at(-1)).toMatchObject({
		x: before.x,
		y: before.y
	});

	// No stuck capture or gesture state: a normal drag works right after.
	await dragChatTitle(page, 70, 30);
	const again = geometryOf(await snapshotOf(page), 'chat');
	expect({ x: again.x, y: again.y }).toEqual({ x: before.x + 70, y: before.y + 30 });
});

test('NT1: losing pointer capture mid-gesture reverts and leaves no stuck state', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const before = geometryOf(await snapshotOf(page), 'chat');

	// Learn the real pointer id of the mouse gesture (browser-defined).
	await page.evaluate(() => {
		const handle = window.__maiWindowDragApi!;
		document.addEventListener(
			'pointerdown',
			(event) => {
				handle.pointerId = event.pointerId;
			},
			{ capture: true, once: true }
		);
	});

	const box = await chatTitlebar(page).boundingBox();
	if (!box) throw new Error('chat title bar is not rendered');
	await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
	await page.mouse.down();
	await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2 + 40, { steps: 3 });

	// Capture lost without up/cancel — the frame reparents under a raise, or
	// the element is removed. Contract: treat as an interruption, revert.
	const lostId = (await probe(page)).pointerId;
	if (lostId === null) throw new Error('pointer id of the gesture was not captured');
	await page.evaluate((pointerId) => {
		document
			.querySelector('[role="dialog"][data-panel-kind="chat"] .mai-window-frame__titlebar')!
			.dispatchEvent(
				new PointerEvent('lostpointercapture', { pointerId, bubbles: true, isPrimary: true })
			);
	}, lostId);

	const reverted = geometryOf(await snapshotOf(page), 'chat');
	expect({ x: reverted.x, y: reverted.y }).toEqual({ x: before.x, y: before.y });

	// The suppressed gesture must not resurrect on later moves.
	await page.mouse.move(box.x + box.width / 2 + 120, box.y + box.height / 2 + 80, { steps: 2 });
	await page.mouse.up();
	expect(geometryOf(await snapshotOf(page), 'chat')).toMatchObject({
		x: before.x,
		y: before.y
	});
});

test('NT1: unmount mid-gesture cleans up — no errors, no ghost emissions, workspace healthy', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);
	const chatId = (await probe(page)).windowIds!.chat;
	const beforeMoves = (await probe(page)).moves.length;

	const box = await chatTitlebar(page).boundingBox();
	if (!box) throw new Error('chat title bar is not rendered');
	await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
	await page.mouse.down();
	await page.mouse.move(box.x + box.width / 2 + 40, box.y + box.height / 2 + 20, { steps: 2 });

	// The controller closes the window mid-gesture: the frame unmounts, the
	// action's destroy runs with a live gesture — and must stay silent.
	const movesAtClose = (await probe(page)).moves.length;
	await page.evaluate(() => window.__maiWindowDragApi!.closeChat());
	await expect(page.getByRole('dialog')).toHaveCount(1);

	await page.mouse.move(box.x + box.width / 2 + 140, box.y + box.height / 2 + 60, { steps: 3 });
	await page.mouse.up();

	expect((await probe(page)).moves.length).toBe(movesAtClose);
	expect((await probe(page)).moves.slice(beforeMoves).every((move) => move.id === chatId)).toBe(
		true
	);
	const snapshot = await snapshotOf(page);
	expect(snapshot.windows.map((window) => window.panel_kind)).toEqual(['model-settings']);
	expect(errors).toEqual([]);

	// The surviving window drags normally — no leaked listeners or capture.
	const prefs = prefsFrame(page);
	const prefsBefore = geometryOf(await snapshotOf(page), 'model-settings');
	const prefsBox = await prefs.locator('.mai-window-frame__titlebar').boundingBox();
	if (!prefsBox) throw new Error('prefs title bar is not rendered');
	await page.mouse.move(prefsBox.x + prefsBox.width / 2, prefsBox.y + prefsBox.height / 2);
	await page.mouse.down();
	await page.mouse.move(prefsBox.x + prefsBox.width / 2 + 50, prefsBox.y + prefsBox.height / 2 + 25, {
		steps: 2
	});
	await page.mouse.up();
	const prefsAfter = geometryOf(await snapshotOf(page), 'model-settings');
	expect({ x: prefsAfter.x, y: prefsAfter.y }).toEqual({
		x: prefsBefore.x + 50,
		y: prefsBefore.y + 25
	});
});

test('NT2: chrome controls, body text and body scroll never initiate a move', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	const chat = chatFrame(page);
	const before = geometryOf(await snapshotOf(page), 'chat');
	const beforePos = await posOf(chat);

	// A plain click on a chrome control is a control interaction: the intent
	// fires, the window does not move.
	const minimize = chat.getByRole('button', { name: 'Minimize' });
	await minimize.click();
	expect((await probe(page)).minimizes).toEqual([before.id]);
	expect(await posOf(chat)).toEqual(beforePos);

	// A press on the control followed by moving away and releasing elsewhere
	// is NOT a click (native press-move-release semantics) and NOT a drag:
	// no additional intent, no movement.
	const minimizeBox = await minimize.boundingBox();
	if (!minimizeBox) throw new Error('minimize button is not rendered');
	await page.mouse.move(minimizeBox.x + minimizeBox.width / 2, minimizeBox.y + minimizeBox.height / 2);
	await page.mouse.down();
	await page.mouse.move(minimizeBox.x + minimizeBox.width / 2 + 60, minimizeBox.y + 10, {
		steps: 3
	});
	await page.mouse.up();
	expect((await probe(page)).minimizes).toEqual([before.id]);
	expect(geometryOf(await snapshotOf(page), 'chat')).toMatchObject({ x: before.x, y: before.y });
	expect(await posOf(chat)).toEqual(beforePos);

	// Body text selects natively and never drags the window.
	const paragraph = chat.locator('[data-sample-text="chat"]');
	await paragraph.click({ clickCount: 3 });
	expect(await page.evaluate(() => window.getSelection()?.toString() ?? '')).toContain(
		'inert sample body text'
	);
	expect(await posOf(chat)).toEqual(beforePos);
	const dragStart = await paragraph.boundingBox();
	if (!dragStart) throw new Error('sample paragraph is not rendered');
	await page.mouse.move(dragStart.x + 10, dragStart.y + 8);
	await page.mouse.down();
	await page.mouse.move(dragStart.x + 80, dragStart.y + 30, { steps: 3 });
	await page.mouse.up();
	expect(await posOf(chat)).toEqual(beforePos);

	// The body is scrollable; scrolling it moves content, not the window.
	await chat.locator('[data-sample-button="chat"]').click();
	expect((await probe(page)).clicks.chat).toBe(1);
	const scrollTop = await chat
		.locator('.mai-window-frame__body')
		.evaluate((el) => {
			el.scrollTop = 120;
			return el.scrollTop;
		});
	expect(scrollTop).toBe(120);
	expect(await posOf(chat)).toEqual(beforePos);
	expect(geometryOf(await snapshotOf(page), 'chat')).toMatchObject({ x: before.x, y: before.y });

	// After all of that, the title bar still drags (controls recovered).
	await dragChatTitle(page, 33, 21);
	const after = geometryOf(await snapshotOf(page), 'chat');
	expect({ x: after.x, y: after.y }).toEqual({ x: before.x + 33, y: before.y + 21 });
});

test('repeated mount cycles do not accumulate handlers; no hidden globals; no domain channel', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);
	const api = await probe(page);
	const chatId = api.windowIds!.chat;
	const before = geometryOf(await snapshotOf(page), 'chat');
	const beforeMoves = api.moves.length;

	// 25 open+close cycles mount and destroy drag actions; a duplicated
	// listener would emit twice per move below.
	await page.evaluate(() => window.__maiWindowDragApi!.cycleThird(25));

	// Focus chat first so the release click is an idempotent no-op and the
	// revision counts exactly the one move emission.
	await chatFrame(page).locator('h2').click();
	await expect(chatFrame(page)).toHaveAttribute('data-focused', 'true');
	const revisionAfterFocus = (await snapshotOf(page)).revision;

	const titlebar = chatTitlebar(page);
	const box = await titlebar.boundingBox();
	if (!box) throw new Error('chat title bar is not rendered');
	await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
	await page.mouse.down();
	await page.mouse.move(box.x + box.width / 2 + 37, box.y + box.height / 2 + 23);
	await page.mouse.up();

	const newMoves = (await probe(page)).moves.slice(beforeMoves);
	expect(newMoves).toHaveLength(1);
	expect(newMoves[0]).toMatchObject({ id: chatId, x: before.x + 37, y: before.y + 23 });
	expect((await snapshotOf(page)).revision).toBe(revisionAfterFocus + 1);
	expect(geometryOf(await snapshotOf(page), 'chat')).toMatchObject({
		x: before.x + 37,
		y: before.y + 23
	});

	// No module-level globals beyond the spec-unique probe handle.
	const extraGlobals = await page.evaluate(() =>
		Object.keys(window).filter((key) => key.startsWith('__mai') && key !== '__maiWindowDragApi')
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
