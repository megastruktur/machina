import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * session-hero-state.spec.ts — CCC S2 proofs: SessionChatPanel's two
 * presentation states inside the centered column.
 *
 * 1. HERO (sessionId === null): ONLY the centered composer — the @service
 *    picker row, the auto-growing textarea (honest placeholder) and Send.
 *    No transcript viewport, no Steer/Pause/Cancel, no generating
 *    indicator, no session-id chrome; the composer block is centered in
 *    the panel.
 * 2. HERO → ACTIVE: submitting through the scripted bag (create + first
 *    turn) swaps to the active layout WITHOUT remounting the composer —
 *    the SAME DOM textarea keeps its mark and focus, the draft is cleared
 *    by the submit, and the optimistic user entry + generating indicator
 *    + control buttons appear.
 * 3. ACTIVE directly: a provided sessionId renders the active layout from
 *    the first client render (no hero flash for a loaded session).
 * 4. Motion honesty: the hero→active spacer transition is a CSS
 *    flex-grow transition (~240ms) that compiles out entirely under
 *    prefers-reduced-motion: reduce — no transition, no animation.
 * 5. 360×640: the hero composer fits without overflow.
 *
 * Deterministic component harness (the reviewed session-service-scope
 * pattern): the REAL SessionChatPanel over a scripted bag with REACTIVE
 * props — the shell's session swap is a prop update, never a remount.
 */

const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const FIXTURE_URL = '/mai-session-hero-fixture/';
const HERO_ENTRY_ID = 'virtual:mai-session-hero-entry';
const HERO_HOST_ID = 'virtual:mai-session-hero-host.svelte';
const PANEL = `${FRONTEND_ROOT}src/lib/panels/SessionChatPanel.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

const CREATED_SESSION_ID = '22222222-2222-4222-8222-222222222222';

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

declare global {
	interface Window {
		__hero?: {
			sessionId: string | null;
			createLog: Array<Record<string, unknown>>;
			turnLog: Array<Record<string, unknown>>;
		};
		__presetSession?: string;
		__mount?(patch: Record<string, unknown>): void;
	}
}

/**
 * The harness host: a Svelte 5 runes component (compiled from a virtual
 * `.svelte` module) holding the bag + reactive state. Prop updates flow
 * into the mounted SessionChatPanel — exactly how the shell swaps the
 * visible session (no unmount/remount, so the composer DOM survives).
 */
function heroHost(): string {
	return `
<script module lang="ts">
	declare global {
		interface Window {
			__presetSession?: string;
		}
	}
</script>
<script lang="ts">
	import SessionChatPanel from ${JSON.stringify(PANEL)};
	import type { ChannelEntry } from ${JSON.stringify(`${FRONTEND_ROOT}src/lib/chat/session_client.js`)};
	import type { SessionRunState } from ${JSON.stringify(`${FRONTEND_ROOT}src/lib/api/client.js`)};

	// The preset is set by the test's addInitScript BEFORE this module
	// loads; guard-narrowed, never an inline cast read.
	const preset = typeof window.__presetSession === 'string' ? window.__presetSession : null;
	let sessionId = $state<string | null>(preset);
	let serverId = $state<string | null>(null);
	let createLog = $state<Record<string, unknown>[]>([]);
	let turnLog = $state<Record<string, unknown>[]>([]);
	let optimisticEntries = $state<ChannelEntry[]>([]);
	let optimisticRunState = $state<{ run_id: string; state: SessionRunState } | null>(null);

	const api = {
		readAgentSession: async (sid: string, scope: string | null) => ({
			// The stored detail names its OWN server (General Consult when
			// the caller sends no scope pin) — never a borrowed selection.
			session_id: sid,
			server_id: scope ?? '00000000-0000-0000-0000-000000000000',
			service_id: null,
			service_name: null,
			source: 'interactive',
			status: 'idle',
			title: null,
			latest_seq: null,
			total_entries: 0,
			compaction_count: 0,
			created_at: '2026-01-01T00:00:00Z',
			updated_at: '2026-01-01T00:00:00Z'
		}),
		createAgentSession: async (payload: Record<string, unknown>) => {
			createLog.push({ ...payload });
			return {
				session_id: ${JSON.stringify(CREATED_SESSION_ID)},
				server_id: payload.server_id,
				service_id: payload.service_id ?? null,
				service_name: null,
				source: 'interactive',
				status: 'idle',
				title: null,
				latest_seq: null,
				total_entries: 0,
				compaction_count: 0,
				created_at: '2026-01-01T00:00:00Z',
				updated_at: '2026-01-01T00:00:00Z'
			};
		},
		submitAgentSessionTurn: async (sid: string, payload: { text: string }) => {
			turnLog.push({ sessionId: sid, text: payload.text });
			return {
				session_id: sid,
				run_id: '33333333-3333-4333-8333-333333333333',
				entry_id: '44444444-4444-4444-8444-444444444444',
				seq: 1,
				run_state: 'queued',
				deduplicated: false
			};
		},
		submitAgentSessionControl: async () => ({ accepted: true })
	};

	window.__hero = {
		get sessionId() {
			return sessionId;
		},
		get createLog() {
			return createLog.map((row) => ({ ...row }));
		},
		get turnLog() {
			return turnLog.map((row) => ({ ...row }));
		}
	};
	window.__mount = (patch: Record<string, unknown>) => {
		const nextSession = patch.sessionId;
		const nextServer = patch.serverId;
		if (typeof nextSession === 'string' || nextSession === null) sessionId = nextSession;
		if (typeof nextServer === 'string' || nextServer === null) serverId = nextServer;
	};
</script>

<SessionChatPanel
	panel={{
		descriptor: {
			schema_version: 1,
			window_id: 'hero-harness',
			panel_kind: 'chat',
			scope: { kind: 'server', server_id: '00000000-0000-0000-0000-000000000000' },
			geometry: { x: 0, y: 0, width: 0, height: 0 },
			presentation: 'normal',
			z: 0,
			title_override: null
		},
		scope: { kind: 'server', server_id: '00000000-0000-0000-0000-000000000000' },
		services: { api, serverOptions: [], availableServices: [] },
		onCleanup: () => () => {}
	}}
	sessionId={sessionId}
	serverId={serverId}
	entries={optimisticEntries}
	optimisticRunState={optimisticRunState}
	onsessionopened={(sid, scope) => {
		// The shell attaches the channel + swaps the visible session
		// synchronously — a reactive prop update, not a remount.
		sessionId = sid;
		serverId = scope;
	}}
	onturnsubmitted={(entry, runState) => {
		// The shell's optimistic merge, in miniature: the resolved submit
		// lands in the visible cache and drives the indicator.
		optimisticEntries = [...optimisticEntries, entry];
		optimisticRunState = runState;
	}}
/>
`;
}

function heroEntry(): string {
	return `
import { mount } from 'svelte';
import HeroHost from ${JSON.stringify(HERO_HOST_ID)};
import ${JSON.stringify(TOKENS)};

mount(HeroHost, { target: document.getElementById('app') });
`;
}

let harnessJs = '';
let harnessCss = '';

function harnessPlugin(hostCode: string): Plugin {
	return {
		name: 'mai-session-hero-harness',
		resolveId(id) {
			if (id === HERO_ENTRY_ID) return `\0${HERO_ENTRY_ID}`;
			if (id === HERO_HOST_ID) return `\0${HERO_HOST_ID}`;
			return undefined;
		},
		load(id) {
			if (id === `\0${HERO_ENTRY_ID}`) return heroEntry();
			if (id === `\0${HERO_HOST_ID}`) return hostCode;
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(heroHost())],
		resolve: { alias: { $lib: `${FRONTEND_ROOT}src/lib` } },
		build: {
			write: false,
			emptyOutDir: true,
			outDir: 'node_modules/.mai-session-hero-fixture',
			rollupOptions: {
				input: HERO_ENTRY_ID,
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
		throw new Error('harness bundle incomplete');
	}
});

async function gotoHarness(page: Page): Promise<void> {
	const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${FIXTURE_URL}demo.css"></head>
<body style="margin:0"><div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script></body></html>`;
	await page.route('**/*', (route: Route) => {
		const requestUrl = new URL(route.request().url());
		if (requestUrl.port !== '4173') return route.continue();
		if (requestUrl.pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (requestUrl.pathname.endsWith('demo.css')) {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
	});
	await page.goto(FIXTURE_URL);
}

test.describe('hero state (no session)', () => {
	test('renders ONLY the centered composer — no viewport, controls, indicator, or header chrome', async ({
		page
	}) => {
		await page.setViewportSize({ width: 1280, height: 720 });
		await gotoHarness(page);
		const panel = page.getByTestId('session-chat-panel');
		await expect(panel).toBeVisible();
		await expect(panel).toHaveAttribute('data-state', 'hero');
		// The composer trio exists…
		await expect(page.getByTestId('session-service-picker')).toBeVisible();
		const input = page.getByTestId('session-composer-input');
		await expect(input).toBeVisible();
		await expect(input).toHaveAttribute(
			'placeholder',
			'Describe the task — a session is created on submit'
		);
		await expect(page.getByTestId('session-composer-submit')).toBeVisible();
		// CCC S3: the unified scope strip carries the hero scope — the
		// server slot (static here: this bag enrolls no options; an
		// interactive select over enrolled options is proven by the scope
		// suite) and the draft @service picker in the service slot.
		await expect(page.getByTestId('chat-scope-strip')).toBeVisible();
		await expect(page.getByTestId('chat-scope-server')).toBeVisible();
		await expect(page.getByTestId('chat-scope-service')).toBeVisible();
		await expect(page.getByTestId('chat-scope-server-select')).toHaveCount(0);
		// …and NOTHING else renders.
		await expect(page.getByTestId('session-history-viewport')).toHaveCount(0);
		await expect(page.getByTestId('session-control-steer')).toHaveCount(0);
		await expect(page.getByTestId('session-control-pause')).toHaveCount(0);
		await expect(page.getByTestId('session-control-cancel')).toHaveCount(0);
		await expect(page.getByTestId('session-generating-indicator')).toHaveCount(0);
		await expect(page.getByTestId('session-header-id')).toHaveCount(0);

		// Centered: the composer block's center ≈ the panel's center (the
		// two hero spacers are symmetric).
		const centers = await page.evaluate(() => {
			const panelEl = document.querySelector('[data-testid="session-chat-panel"]')!;
			const composerEl = document.querySelector('.mai-session-chat__composer')!;
			const pr = panelEl.getBoundingClientRect();
			const cr = composerEl.getBoundingClientRect();
			return {
				panel: { cx: pr.x + pr.width / 2, cy: pr.y + pr.height / 2 },
				composer: { cx: cr.x + cr.width / 2, cy: cr.y + cr.height / 2 }
			};
		});
		expect(Math.abs(centers.composer.cx - centers.panel.cx)).toBeLessThanOrEqual(2);
		expect(Math.abs(centers.composer.cy - centers.panel.cy)).toBeLessThanOrEqual(2);
	});

	test('the textarea auto-grows modestly and caps at ~6 rows with internal scroll', async ({
		page
	}) => {
		await gotoHarness(page);
		const input = page.getByTestId('session-composer-input');
		await expect(input).toBeVisible();
		const base = await input.evaluate((el) => el.getBoundingClientRect().height);
		await input.fill(Array.from({ length: 12 }, () => 'a full line of text').join('\n'));
		const grown = await input.evaluate((el) => ({
			height: el.getBoundingClientRect().height,
			overflowY: getComputedStyle(el).overflowY
		}));
		// Modest growth past the 2-row baseline…
		expect(grown.height).toBeGreaterThan(base);
		// …capped at ~6 rows (12 typed lines do NOT grow past the cap), with
		// the overflow handled INSIDE the textarea.
		const lineHeight = await input.evaluate((el) =>
			parseFloat(getComputedStyle(el).lineHeight)
		);
		expect(grown.height).toBeLessThanOrEqual(lineHeight * 6 + 24);
		expect(['auto', 'scroll']).toContain(grown.overflowY);
	});
});

test.describe('hero → active transition', () => {
	test('first submit switches to active without remounting the composer', async ({ page }) => {
		await gotoHarness(page);
		const input = page.getByTestId('session-composer-input');
		await expect(input).toBeVisible();
		// Mark the live DOM node: the SAME textarea must survive the switch.
		await input.evaluate((el) => el.setAttribute('data-hero-mark', 'survivor'));
		await input.click();
		await page.keyboard.type('launch the probe');
		await page.keyboard.press('Enter');

		// Active layout: viewport, optimistic entry, indicator, controls.
		await expect(page.getByTestId('session-chat-panel')).toHaveAttribute('data-state', 'active');
		await expect(page.getByTestId('session-history-viewport')).toBeVisible();
		await expect(page.getByTestId('entry-user-message')).toContainText('launch the probe');
		await expect(page.getByTestId('session-generating-indicator')).toBeVisible();
		await expect(page.getByTestId('session-control-steer')).toBeVisible();
		await expect(page.getByTestId('session-control-pause')).toBeVisible();
		await expect(page.getByTestId('session-control-cancel')).toBeVisible();
		// The committed session closes the draft picker (no silent rebind).
		await expect(page.getByTestId('session-service-picker')).toHaveCount(0);

		// The bag saw the create + the first turn.
		await expect
			.poll(() => page.evaluate(() => window.__hero!.createLog.length))
			.toBe(1);
		await expect
			.poll(() => page.evaluate(() => window.__hero!.turnLog.length))
			.toBe(1);
		await expect
			.poll(() => page.evaluate(() => window.__hero!.sessionId))
			.toBe(CREATED_SESSION_ID);

		// Same DOM node (marked), still holding focus; the draft was
		// cleared by the submit exactly as before.
		const survivor = page.getByTestId('session-composer-input');
		await expect(survivor).toHaveAttribute('data-hero-mark', 'survivor');
		await expect(survivor).toHaveValue('');
		const focused = await page.evaluate(
			() => document.activeElement?.getAttribute('data-testid') ?? null
		);
		expect(focused).toBe('session-composer-input');
	});

	test('a provided sessionId renders the active layout directly (no hero flash)', async ({
		page
	}) => {
		await page.addInitScript(
			(sid) => {
				window.__presetSession = sid;
			},
			'11111111-1111-4111-8111-111111111111'
		);
		await gotoHarness(page);
		const panel = page.getByTestId('session-chat-panel');
		await expect(panel).toBeVisible();
		// The state is active from the FIRST client render.
		await expect(panel).toHaveAttribute('data-state', 'active');
		await expect(page.getByTestId('session-history-viewport')).toBeVisible();
		// The sr-only session id stays in the DOM for the E2E assertions.
		await expect(page.getByTestId('session-header-id')).toBeVisible();
		await expect(page.getByTestId('session-control-steer')).toBeVisible();
		await expect(page.getByTestId('session-service-picker')).toHaveCount(0);
		// CCC S3: the active strip renders the stored read-only scope
		// (this bag's detail read yields the session's own server id).
		await expect(page.getByTestId('chat-scope-strip')).toBeVisible();
		await expect(page.getByTestId('chat-scope-server')).toContainText('General Consult');
		await expect(page.getByTestId('chat-scope-service')).toHaveCount(0);
		await expect(page.getByTestId('session-history-empty')).toBeVisible();
	});
});

test.describe('motion honesty', () => {
	test('the spacer transition exists by default and compiles out under reduced motion', async ({
		page
	}) => {
		// Default: the hero→active transition is a real (bounded) CSS
		// flex-grow transition on the layout spacers.
		await gotoHarness(page);
		const withMotion = await page.evaluate(() => {
			const spacer = document.querySelector('.mai-session-chat__spacer')!;
			const cs = getComputedStyle(spacer);
			return { duration: cs.transitionDuration, property: cs.transitionProperty };
		});
		expect(parseFloat(withMotion.duration)).toBeGreaterThan(0);
		expect(withMotion.property).toContain('flex-grow');

		// Reduced motion: NO effective transition and NO animation anywhere
		// in the panel — the layout switch is instant. (tokens.css forces
		// 0.01ms !important durations under reduce; anything under 1ms IS
		// "no transition" for an observer. Durations normalize ms → s.)
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await gotoHarness(page);
		const reduced = await page.evaluate(() => {
			const panel = document.querySelector('[data-testid="session-chat-panel"]')!;
			const animated = [...panel.querySelectorAll('*')]
				.filter((el) => {
					const cs = getComputedStyle(el);
					const raw = cs.transitionDuration;
					return (
						parseFloat(raw) * (raw.includes('ms') ? 0.001 : 1) >= 0.001 ||
						cs.animationName !== 'none'
					);
				})
				.map((el) => el.getAttribute('class') ?? el.tagName);
			const spacer = panel.querySelector('.mai-session-chat__spacer')!;
			const spacerRaw = getComputedStyle(spacer).transitionDuration;
			return {
				spacerDuration: spacerRaw,
				spacerSeconds: parseFloat(spacerRaw) * (spacerRaw.includes('ms') ? 0.001 : 1),
				animated
			};
		});
		expect(reduced.spacerSeconds).toBeLessThan(0.001);
		expect(reduced.animated).toEqual([]);
	});
});

test.describe('360x640 bounds', () => {
	test('the hero composer fits without overflow', async ({ page }) => {
		await page.setViewportSize({ width: 360, height: 640 });
		await gotoHarness(page);
		await expect(page.getByTestId('session-composer-input')).toBeVisible();
		const overflow = await page.evaluate(() => {
			const input = document
				.querySelector('[data-testid="session-composer-input"]')!
				.getBoundingClientRect();
			return {
				scroll: document.documentElement.scrollWidth,
				client: document.documentElement.clientWidth,
				inputRight: input.right,
				inputBottom: input.bottom
			};
		});
		expect(overflow.scroll).toBeLessThanOrEqual(overflow.client);
		expect(overflow.inputRight).toBeLessThanOrEqual(360);
		expect(overflow.inputBottom).toBeLessThanOrEqual(640);
	});
});
