import { expect, test, type Locator, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-window-frame — real-browser proofs for the reusable non-modal window
 * chrome (frontend/src/lib/workspace/WindowFrame.svelte).
 *
 * Focused per the test policy: the frame is exercised in the real Chromium
 * browser against the reviewed 01-web component fixture
 * (tests/fixtures/component-host.svelte) — no backend, no invented API.
 *
 * Harness (self-contained; the brief's allowlist covers only this spec and
 * the component, so no fixture files or config edits are committed):
 *  1. test.beforeAll runs an IN-MEMORY Vite production build (write: false)
 *     of two virtual modules — a root component (compiled .svelte) that owns
 *     the thin fixture controller (the real `createWorkspaceState` from the
 *     integrated 01-window-state slice, two validated opens, subscription →
 *     focused/z props, intent callbacks → state transitions), and an entry
 *     that mounts it inside the reviewed component host.
 *  2. Each test serves the built JS/CSS/HTML through Playwright route
 *     interception, so the frame runs as real compiled Svelte 5 code.
 *
 * Scenario coverage (brief "feature-gate coverage", runtime owner 01-verify):
 *  - two actual frame instances with labeled inert sample content
 *  - non-modal dialog semantics (role/aria-modal/labelledby + names)
 *  - keyboard/tab between title bar and every control; keyboard raise
 *  - focus stacking and close affect only the selected view
 *  - content buttons and text selection remain usable; no drag side effects
 *  - close is view-only intent emission (frame never removes itself)
 */

const FIXTURE_URL = '/mai-window-frame-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const APP_ID = 'virtual:mai-window-frame-app.svelte';
const ENTRY_ID = 'virtual:mai-window-frame-entry.js';

const FRAME = `${FRONTEND_ROOT}src/lib/workspace/WindowFrame.svelte`;
const STATE = `${FRONTEND_ROOT}src/lib/workspace/state.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const RUN_ID_A = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';

/**
 * Registry stand-ins until 01-panel-registry lands the frozen registry: the
 * display titles are explicit fixture vocabulary, never derived in the frame
 * from panel_kind.
 */
const APP_SOURCE = `
<script>
	/**
	 * Thin fixture controller (the future workspace-runtime role): opens two
	 * validated windows in the real state layer and renders one frame per
	 * snapshot. Deliberately RUNE-FREE (writable store + store
	 * auto-subscription only, no state/props runes): virtual .svelte modules
	 * compile in LEGACY mode inside this in-memory production build (observed
	 * 2026-10-13 — rune calls survive as legacy store accessors and throw at
	 * runtime; recorded as a harness-convention finding in the card evidence).
	 * Sources that use no runes compile identically under either compiler
	 * mode, so the harness does not depend on plugin behavior. The writable
	 * here is per-navigation fixture state inside the test harness — it is
	 * not the workspace layer and never ships.
	 */
	import WindowFrame from '${FRAME}';
	import { createWorkspaceState } from '${STATE}';
	import ComponentHost from '${HOST}';
	import { writable } from 'svelte/store';

	const api = window.__maiFrameApi;

	const state = createWorkspaceState({
		knownPanelKinds: new Set(['chat', 'model-settings']),
		instanceKey: () => null
	});

	// Registry stand-ins until 01-panel-registry lands (display title keys).
	const CHAT_TITLE = 'Chat';
	const GLOBAL_TITLE = 'Model settings';

	const chat = state.open({
		panel_kind: 'chat',
		scope: {
			kind: 'server',
			server_id: '${SERVER_A}',
			object: { kind: 'run', id: '${RUN_ID_A}' }
		},
		geometry: { x: 32, y: 24, width: 560, height: 360 }
	});
	const global = state.open({
		panel_kind: 'model-settings',
		scope: { kind: 'global' },
		geometry: { x: 640, y: 96, width: 400, height: 280 },
		title_override: 'Preferences'
	});
	if (!chat.ok || !global.ok) throw new Error('fixture opens must validate');

	api.windowIds = {
		chat: chat.descriptor.window_id,
		global: global.descriptor.window_id
	};
	api.snapshot = () => state.snapshot();

	// Display-label stand-ins keyed by identity (the registry's job later).
	const titles = {
		[chat.descriptor.window_id]: CHAT_TITLE,
		[global.descriptor.window_id]: GLOBAL_TITLE
	};

	// Server-scope display label stand-in (the runtime's resolution job later).
	// Passed to BOTH windows on purpose: the global window must still read
	// "Global", never a server name (ADR §3.2).
	const contextLabel = 'Server Alpha';

	// Stable DOM order (open order): raising a window must NOT reparent its
	// subtree — Chromium blurs the focused element when its ancestor chain is
	// moved (observed 2026-10-13: keyboard entry into an inactive window
	// raised it, the reparent blurred the just-focused title bar, and every
	// subsequent Tab ping-ponged the raise between windows). Visual stacking
	// is descriptor.z; this fixture keeps tab order = open order (the future
	// workspace runtime owns the real DOM-order policy).
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

<ComponentHost label="MAI window frame fixture">
	<div
		class="mai-desktop"
		data-testid="desktop"
		style="position: relative; width: 1100px; height: 640px;"
	>
		{#each $snapshotStore.windows as descriptor (descriptor.window_id)}
			<WindowFrame
				{descriptor}
				title={titles[descriptor.window_id] ?? descriptor.panel_kind}
				{contextLabel}
				focused={$snapshotStore.focused_id === descriptor.window_id}
				onactivate={(id) => {
					api.activate.push(id);
					// The controller's response to an activation intent: raise the
					// window in the real state layer (idempotent — focusing the
					// already-focused window is a no-op).
					state.focus(id);
				}}
				onminimize={(id) => api.minimize.push(id)}
				ontogglemaximize={(id) => api.maximize.push(id)}
				onclose={(id) => {
					api.close.push(id);
					// Only the global window's controller performs the close; the
					// chat window's controller declines — proving the frame's
					// close is view-only intent emission with no self-removal.
					if (id === global.descriptor.window_id) state.close(id);
				}}
			>
				{#snippet controls()}
					<button type="button" data-chrome-extra={descriptor.panel_kind}>
						Details
					</button>
				{/snippet}
				<div class="sample" data-sample={descriptor.panel_kind}>
					<p data-sample-text={descriptor.panel_kind}>
						{titles[descriptor.window_id] ?? descriptor.panel_kind} inert
						sample body text — selectable fixture content.
					</p>
					<button type="button" data-sample-button={descriptor.panel_kind}>
						{titles[descriptor.window_id] ?? descriptor.panel_kind} action
					</button>
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
	activate: [],
	minimize: [],
	maximize: [],
	close: [],
	clicks: {},
	windowIds: null,
	snapshot: null
};
window.__maiFrameApi = api;

// Inert sample content: count interactions by delegation so the raw fixture
// content stays event-free and the tests read native click outcomes only.
document.addEventListener('click', (event) => {
	if (!(event.target instanceof Element)) return;
	const sample = event.target.closest('[data-sample-button]');
	if (sample) {
		const key = sample.getAttribute('data-sample-button') ?? '';
		api.clicks[key] = (api.clicks[key] ?? 0) + 1;
	}
	const extra = event.target.closest('[data-chrome-extra]');
	if (extra) {
		const key = 'chrome-' + (extra.getAttribute('data-chrome-extra') ?? '');
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

/** Descriptor shape as it crosses the CDP boundary (schema-v1 whitelist). */
interface ProbeWindow {
	window_id: string;
	panel_kind: string;
	presentation: string;
	z: number;
	geometry: { x: number; y: number; width: number; height: number };
	scope: { kind: string; server_id?: string };
}

interface ProbeSnapshot {
	revision: number;
	windows: ProbeWindow[];
	focused_id: string | null;
}

/** Shape the virtual entry exposes on `window` for the probes below. */
interface ProbeApi {
	activate: string[];
	minimize: string[];
	maximize: string[];
	close: string[];
	clicks: Record<string, number>;
	windowIds: { chat: string; global: string } | null;
	/** Only callable IN-PAGE (functions do not cross the CDP boundary). */
	snapshot(): ProbeSnapshot;
}

declare global {
	interface Window {
		/** Spec-unique probe global (no collision with the sibling specs). */
		__maiFrameApi?: ProbeApi;
	}
}

let harnessJs = '';
let harnessCss = '';

/** Serves the virtual modules from the in-memory bundle; nothing hits disk. */
function harnessPlugin(appSource: string, entrySource: string): Plugin {
	return {
		name: 'mai-window-frame-harness',
		resolveId(id) {
			// The .svelte virtual module must NOT get the conventional `\0`
			// prefix: vite-plugin-svelte excludes `\0` ids from Svelte
			// compilation, so it must stay a plain virtual id to be compiled.
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
			outDir: 'node_modules/.mai-window-frame-fixture',
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
<title>MAI window frame fixture</title>
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

/** Reads the current probe API (fresh module state per navigation). */
async function probe(page: Page): Promise<ProbeApi> {
	const value = await page.evaluate(() => window.__maiFrameApi);
	if (!value) throw new Error('fixture probe api is missing');
	return value;
}

/**
 * Reads the workspace snapshot by calling the controller in-page — functions
 * cannot cross the CDP boundary, so the call itself happens in the browser.
 */
async function snapshotOf(page: Page): Promise<ProbeSnapshot> {
	return page.evaluate(() => window.__maiFrameApi!.snapshot());
}

/** Computed box of a frame (including z-index) from the real DOM. */
function boxOf(frame: Locator): Promise<Record<string, string>> {
	return frame.evaluate((el) => {
		const style = getComputedStyle(el);
		return {
			left: style.left,
			top: style.top,
			width: style.width,
			height: style.height,
			zIndex: style.zIndex
		};
	});
}

/** Computed position/size of a frame (no z-index — stacking changes only). */
function posOf(frame: Locator): Promise<Record<string, string>> {
	return frame.evaluate((el) => {
		const style = getComputedStyle(el);
		return {
			left: style.left,
			top: style.top,
			width: style.width,
			height: style.height
		};
	});
}

/**
 * Frame locators keyed by panel kind — the spec's single definition of "the
 * chat window" vs "the prefs window", used in lockstep by every test below.
 */
const chatFrame = (page: Page) => page.locator('[role="dialog"][data-panel-kind="chat"]');
const prefsFrame = (page: Page) =>
	page.locator('[role="dialog"][data-panel-kind="model-settings"]');

test('two frames render as labeled non-modal dialogs with contextual badges and rendered geometry', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);

	// Real client mount inside the reviewed component-host fixture.
	const host = page.getByTestId('component-host');
	await expect(host).toHaveAttribute('data-mounted', 'true');
	await expect(host).toBeVisible();

	const chat = chatFrame(page);
	const prefs = prefsFrame(page);
	await expect(chat).toBeVisible();
	await expect(prefs).toBeVisible();

	// Non-modal dialogs per ADR §6 — explicit aria-modal="false" on both.
	await expect(chat).toHaveAttribute('aria-modal', 'false');
	await expect(prefs).toHaveAttribute('aria-modal', 'false');

	// Accessible name "«panel title» — «context label»"; the title_override
	// replaces the panel title and the global scope always reads "Global"
	// even though the fixture passed a server label to both windows (ADR §3.2).
	await expect(chat).toHaveAccessibleName('Chat — Server Alpha');
	await expect(prefs).toHaveAccessibleName('Preferences — Global');

	// aria-labelledby resolves to the visible heading on each instance, and
	// the generated title ids are unique per instance (no shared module ids).
	const labelIds: string[] = await page.$$eval('[role="dialog"]', (dialogs) =>
		dialogs.map((dialog) => dialog.getAttribute('aria-labelledby') ?? '')
	);
	expect(labelIds).toHaveLength(2);
	expect(new Set(labelIds).size).toBe(2);
	for (const id of labelIds) {
		await expect(page.locator(`[role="dialog"] h2[id="${id}"]`)).toBeVisible();
	}

	// Both frames mount inside the reviewed fixture host, below the desktop.
	for (const frame of [chat, prefs]) {
		const insideHost = await frame.evaluate(
			(el) => el.closest('[data-testid="component-host"]') !== null
		);
		expect(insideHost).toBe(true);
	}

	// Identity and descriptor data flow one way: DOM reflects the snapshot.
	const api = await probe(page);
	const chatId = api.windowIds!.chat;
	const globalId = api.windowIds!.global;
	expect(new Set([chatId, globalId]).size).toBe(2);
	await expect(chat).toHaveAttribute('data-window-id', chatId);
	await expect(prefs).toHaveAttribute('data-window-id', globalId);
	await expect(chat).toHaveAttribute('data-presentation', 'normal');
	await expect(prefs).toHaveAttribute('data-presentation', 'normal');

	// Geometry and z render from the descriptor (opened order: chat, global).
	expect(await boxOf(chat)).toEqual({
		left: '32px',
		top: '24px',
		width: '560px',
		height: '360px',
		zIndex: '1'
	});
	expect(await boxOf(prefs)).toEqual({
		left: '640px',
		top: '96px',
		width: '400px',
		height: '280px',
		zIndex: '2'
	});

	expect(errors).toEqual([]);
});

test('active focus styling follows the controller focus; activation affects only the selected view', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const chat = chatFrame(page);
	const prefs = prefsFrame(page);
	const api = await probe(page);
	const chatId = api.windowIds!.chat;
	const globalId = api.windowIds!.global;

	// The last open (prefs) is focused: focused styling on prefs only, and
	// the difference is actually visible (border, title voice, badge colors).
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await expect(chat).toHaveAttribute('data-focused', 'false');
	const focusedBorder = await prefs.evaluate((el) => getComputedStyle(el).borderTopColor);
	const unfocusedBorder = await chat.evaluate((el) => getComputedStyle(el).borderTopColor);
	// The harness imports tokens.css with the encapsulated mechanicus theme
	// as the default palette (mui-01): primary is the red sanctum accent,
	// the unfocused border the dark panel line.
	expect(focusedBorder).toBe('rgb(185, 28, 28)'); // --mai-color-primary #b91c1c
	expect(unfocusedBorder).toBe('rgb(42, 50, 65)'); // --mai-color-border #2a3241
	const focusedTitle = await prefs.evaluate((el) =>
		getComputedStyle(el.querySelector('.mai-window-frame__title-text')!).color
	);
	const unfocusedTitle = await chat.evaluate((el) =>
		getComputedStyle(el.querySelector('.mai-window-frame__title-text')!).color
	);
	expect(focusedTitle).not.toBe(unfocusedTitle);

	// Activating the inactive window by clicking its content button: the
	// click BOTH raises the window (PT2) AND runs the control (no swallow).
	await page.getByRole('button', { name: 'Chat action' }).click();
	const after = await probe(page);
	// Pointerdown + focusin may both emit for one gesture; the contract is
	// "activation names the right window, idempotent at the controller".
	expect(after.activate.filter((id) => id !== chatId)).toEqual([]);
	expect(after.clicks.chat).toBe(1);
	expect(after.close).toEqual([]);
	expect(after.minimize).toEqual([]);
	expect(after.maximize).toEqual([]);

	await expect(chat).toHaveAttribute('data-focused', 'true');
	await expect(prefs).toHaveAttribute('data-focused', 'false');
	expect(await boxOf(chat)).toMatchObject({ zIndex: '2' });
	expect(await boxOf(prefs)).toMatchObject({ zIndex: '1' });

	// Only the selected view changed: prefs keeps its geometry and identity.
	expect(await posOf(prefs)).toEqual({ left: '640px', top: '96px', width: '400px', height: '280px' });
	await expect(prefs).toHaveAttribute('data-window-id', globalId);
	await expect(prefs).toHaveAttribute('data-presentation', 'normal');

	// Activating prefs by clicking its title bar swaps focus back.
	await prefs.locator('h2').click();
	const back = await probe(page);
	expect(back.activate.filter((id) => id !== chatId && id !== globalId)).toEqual([]);
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await expect(chat).toHaveAttribute('data-focused', 'false');
	expect(await boxOf(prefs)).toMatchObject({ zIndex: '2' });
	expect(await boxOf(chat)).toMatchObject({ zIndex: '1' });
});

test('keyboard reaches title bar, slot control and every labeled control; keyboard activation raises the window', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const chat = chatFrame(page);
	const prefs = prefsFrame(page);
	const api = await probe(page);
	const globalId = api.windowIds!.global;

	/** Current tab stop as seen by the browser (DOM order, no shortcuts). */
	const stop = () =>
		page.evaluate(() => ({
			tag: document.activeElement?.tagName ?? null,
			panel:
				document.activeElement?.closest('[role="dialog"]')?.getAttribute('data-panel-kind') ??
				null,
			label: document.activeElement?.getAttribute('aria-label'),
			text: (document.activeElement?.textContent ?? '').trim()
		}));

	// First Tab lands on the bottom window's focusable title bar (a fresh
	// page occasionally delivers the first Tab to the browser UI — bounded
	// retry until focus is inside the page).
	let landedOnChatTitle = false;
	for (let i = 0; i < 4 && !landedOnChatTitle; i += 1) {
		await page.keyboard.press('Tab');
		const current = await stop();
		landedOnChatTitle = current.tag === 'H2' && current.panel === 'chat';
	}
	expect(landedOnChatTitle, 'first Tab must reach the bottom window title bar').toBe(true);
	// …and keyboard entry activates the window (focusin → raise, PT2).
	await expect(chat).toHaveAttribute('data-focused', 'true');

	// Tab order follows DOM order: chrome slot, labeled controls, then body.
	await page.keyboard.press('Tab');
	expect(await stop()).toMatchObject({ panel: 'chat', text: 'Details' });
	await page.keyboard.press('Tab');
	expect(await stop()).toMatchObject({ panel: 'chat', label: 'Minimize' });
	await page.keyboard.press('Tab');
	expect(await stop()).toMatchObject({ panel: 'chat', label: 'Maximize' });
	await page.keyboard.press('Tab');
	expect(await stop()).toMatchObject({ panel: 'chat', label: 'Close' });
	await page.keyboard.press('Tab');
	expect(await stop()).toMatchObject({ panel: 'chat', text: 'Chat action' });

	// Native keyboard activation works on body and chrome controls alike.
	await page.keyboard.press('Enter');
	expect((await probe(page)).clicks.chat).toBe(1);

	// Tabbing into the other (inactive) window raises it the same way. The
	// DOM order is stacking order, so the walk from the raised window can
	// wrap — tab until the prefs title bar is reached (bounded).
	let reachedPrefsTitle = false;
	for (let i = 0; i < 10 && !reachedPrefsTitle; i += 1) {
		await page.keyboard.press('Tab');
		const current = await stop();
		reachedPrefsTitle = current.tag === 'H2' && current.panel === 'model-settings';
	}
	expect(reachedPrefsTitle, 'tab order must reach the other window title bar').toBe(true);
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await expect(chat).toHaveAttribute('data-focused', 'false');

	await page.keyboard.press('Tab'); // prefs chrome slot control
	expect(await stop()).toMatchObject({ panel: 'model-settings', text: 'Details' });
	await page.keyboard.press('Tab');
	expect(await stop()).toMatchObject({ panel: 'model-settings', label: 'Minimize' });
	await page.keyboard.press('Enter');
	expect((await probe(page)).minimize).toEqual([globalId]);
	expect((await probe(page)).close).toEqual([]);
});

test('body content stays fully usable: repeated clicks, text selection, no drag side effects', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const chat = chatFrame(page);
	const before = await posOf(chat);

	// Content buttons keep working across activation changes.
	const chatButton = page.getByRole('button', { name: 'Chat action' });
	await chatButton.click();
	await chatButton.click();
	expect((await probe(page)).clicks.chat).toBe(2);

	// Text selection inside the body is native and unaffected by the chrome.
	const paragraph = chat.locator('[data-sample-text="chat"]');
	await paragraph.click({ clickCount: 3 });
	const selected = await page.evaluate(() => window.getSelection()?.toString() ?? '');
	expect(selected).toContain('inert sample body text');

	// Pointer-down + move over the title bar initiates nothing: the frame
	// holds no drag logic and geometry only ever changes via the controller.
	const box = await chat.boundingBox();
	if (!box) throw new Error('chat frame is not rendered');
	await page.mouse.move(box.x + box.width / 2, box.y + 14);
	await page.mouse.down();
	await page.mouse.move(box.x + box.width / 2 + 60, box.y + 40, { steps: 6 });
	await page.mouse.up();
	expect(await posOf(chat)).toEqual(before);
	expect(await posOf(prefsFrame(page))).toEqual({
		left: '640px',
		top: '96px',
		width: '400px',
		height: '280px'
	});
});

test('chrome controls are view-only intents; close affects only its own view', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	const chat = chatFrame(page);
	const prefs = prefsFrame(page);
	const api = await probe(page);
	const chatId = api.windowIds!.chat;
	const globalId = api.windowIds!.global;

	// Minimize/maximize emit intents and nothing else — the frame renders
	// presentation, it never applies transitions (01-window-presentation).
	await chat.getByRole('button', { name: 'Maximize' }).click();
	expect((await probe(page)).maximize).toEqual([chatId]);
	await expect(chat).toHaveAttribute('data-presentation', 'normal');
	await expect(page.getByRole('dialog')).toHaveCount(2);
	await chat.getByRole('button', { name: 'Minimize' }).click();
	expect((await probe(page)).minimize).toEqual([chatId]);
	await expect(page.getByRole('dialog')).toHaveCount(2);

	// Close on the chat window: the controller declines — the frame must NOT
	// remove itself (view-only emission; unmounting is the controller's job).
	await chat.getByRole('button', { name: 'Close' }).click();
	expect((await probe(page)).close).toEqual([chatId]);
	await expect(page.getByRole('dialog')).toHaveCount(2);
	await expect(chat).toBeVisible();
	await expect(prefs).toBeVisible();

	// Close on the focused prefs window: the controller performs the close
	// through the real state layer — only that view leaves the workspace.
	await prefs.getByRole('button', { name: 'Close' }).click();
	expect((await probe(page)).close).toEqual([chatId, globalId]);
	await expect(page.getByRole('dialog')).toHaveCount(1);

	// The surviving window is intact and fully functional: identity, focus
	// (handed to the new top window) and its content controls all still work.
	const snapshot = await snapshotOf(page);
	expect(snapshot.windows.map((w) => w.panel_kind)).toEqual(['chat']);
	expect(snapshot.focused_id).toBe(chatId);
	await expect(chat).toHaveAttribute('data-focused', 'true');
	await expect(chat).toHaveAttribute('data-window-id', chatId);
	await page.getByRole('button', { name: 'Chat action' }).click();
	expect((await probe(page)).clicks.chat).toBe(1);
	expect((await probe(page)).close).toEqual([chatId, globalId]);
});

test('no hidden globals and no domain channel: page loads fixture assets only', async ({ page }) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);
	await expect(page.getByRole('dialog')).toHaveCount(2);

	// The fixture never leaves its route: any frame-made domain request
	// (fetch/XHR/WebSocket/EventSource) would show up as a cross-route
	// resource entry — the chrome layer has no such channel.
	const resources: string[] = await page.evaluate(() =>
		performance.getEntriesByType('resource').map((entry) => new URL(entry.name).pathname)
	);
	expect(resources.length).toBeGreaterThan(0);
	expect(resources.every((pathname) => pathname.startsWith(FIXTURE_URL))).toBe(true);

	// No module-level frame globals beyond the spec-unique probe handle.
	const extraGlobals = await page.evaluate(() =>
		Object.keys(window).filter((key) => key.startsWith('__mai') && key !== '__maiFrameApi')
	);
	expect(extraGlobals).toEqual([]);
	expect(errors).toEqual([]);
});
