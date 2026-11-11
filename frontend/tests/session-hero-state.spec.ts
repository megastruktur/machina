import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * session-hero-state.spec.ts — CCC S2/CSP P1/CHC H1 proofs: the hero
 * (no session) surface is a COMPACT card that hugs ONLY the composer and
 * is centered in the stage; submitting the first turn flips the SAME
 * surface (one composer DOM node — focus + draft survive) to the
 * full-height active layout with the transcript above the bottom-pinned
 * input, animated by the shell's ~240ms card growth (reduced-motion:
 * instant); a provided sessionId renders active full-height directly.
 *
 * The harness mirrors the shell's dock structure (transparent
 * `.chc-dock` positioning box + blended `.chc-dock__card` surface +
 * WAAPI growth effects) exactly as DesktopShell ships it — the real
 * shell's own card geometry is proven by desktop-shell.spec.ts against
 * the production route.
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
 * `.svelte` module) holding the bag + reactive state, wrapping the panel
 * in the shell-mirroring dock card. Prop updates flow into the mounted
 * SessionChatPanel — exactly how the shell swaps the visible session
 * (no unmount/remount, so the composer DOM survives).
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

	// --- CHC H1 mirror of DesktopShell's dock card growth -------------------
	// The SHELL owns this logic; the mirror keeps the fixture honest so
	// the growth proofs exercise the same mechanism the route ships.
	let cardEl: HTMLElement | null = null;
	let motionReduced = $state(false);
	$effect(() => {
		const query = window.matchMedia('(prefers-reduced-motion: reduce)');
		const apply = () => (motionReduced = query.matches);
		apply();
		query.addEventListener('change', apply);
		return () => query.removeEventListener('change', apply);
	});
	const CARD_GROWTH_MS = 240;
	let cardFlipFrom: DOMRect | null = null;
	let wasHero: boolean | null = null;
	$effect.pre(() => {
		const hero = sessionId === null;
		if (wasHero === true && !hero && cardEl !== null) {
			cardFlipFrom = cardEl.getBoundingClientRect();
		}
		wasHero = hero;
	});
	let cardAnimation: Animation | null = null;
	$effect(() => {
		void sessionId;
		const card = cardEl;
		const from = cardFlipFrom;
		cardFlipFrom = null;
		if (card === null || from === null || motionReduced) return;
		const to = card.getBoundingClientRect();
		if (Math.abs(to.height - from.height) < 2) return;
		cardAnimation?.cancel();
		cardAnimation = card.animate(
			[{ height: \`\${from.height}px\` }, { height: \`\${to.height}px\` }],
			{ duration: CARD_GROWTH_MS, easing: 'ease' }
		);
	});
</script>

<div class="chc-dock" class:chc-dock--hero={sessionId === null}>
	<div class="chc-dock__card" bind:this={cardEl}>
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
	</div>
</div>

<style>
	/* Mirror of DesktopShell's .mai-desktop-shell__dock: the transparent
	   absolute positioning host (stage-inset, width-bounded). */
	.chc-dock {
		position: absolute;
		top: var(--mai-space-2);
		bottom: var(--mai-space-3);
		left: 0;
		right: 0;
		margin-inline: auto;
		width: min(880px, 72%, calc(100% - 2 * var(--mai-space-3)));
		min-height: 0;
		display: flex;
		flex-direction: column;
		padding: var(--mai-hud-pad-y) var(--mai-hud-pad-x);
		pointer-events: none;
	}
	@media (max-width: 720px) {
		.chc-dock {
			width: calc(100% - 2 * var(--mai-space-3));
		}
	}
	/* Mirror of .mai-desktop-shell__dock-body: the blended CARD — active
	   fills (explicit height:100%, what the growth animates), hero hugs
	   the composer and centers via auto block margins. */
	.chc-dock__card {
		pointer-events: auto;
		flex: 0 0 auto;
		margin-block: auto;
		height: 100%;
		min-height: 0;
		min-width: 0;
		background: var(--mai-color-surface);
		border: var(--mai-border-width) solid var(--mai-color-border);
		border-radius: var(--mai-radius-2xl, var(--mai-radius-lg));
		box-shadow: none;
	}
	.chc-dock--hero .chc-dock__card {
		height: auto;
	}
</style>
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
	// CHC H1: #app is the STAGE (definite height, positioning context)
	// exactly like .mai-desktop-shell__stage; the harness dock card
	// inside it behaves like the shell's column card.
	const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${FIXTURE_URL}demo.css"></head>
<body style="margin:0"><div id="app" style="height:100dvh; position:relative"></div>
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

type CardGeometry = {
	dock: { top: number; bottom: number; height: number; width: number; cx: number; contentTop: number; contentBottom: number };
	card: { top: number; bottom: number; height: number; width: number; cx: number; cy: number };
	panel: { height: number };
	composer: { height: number; bottom: number; cx: number };
};

async function cardGeometry(page: Page): Promise<CardGeometry> {
	return page.evaluate(() => {
		const dockEl = document.querySelector('.chc-dock')!;
		const cardEl = document.querySelector('.chc-dock__card')!;
		const panelEl = document.querySelector('[data-testid="session-chat-panel"]')!;
		const composerEl = document.querySelector('.mai-session-chat__composer')!;
		const dc = getComputedStyle(dockEl);
		const dr = dockEl.getBoundingClientRect();
		const cr = cardEl.getBoundingClientRect();
		const pr = panelEl.getBoundingClientRect();
		const mr = composerEl.getBoundingClientRect();
		const padTop = parseFloat(dc.paddingTop);
		const padBottom = parseFloat(dc.paddingBottom);
		return {
			dock: {
				top: dr.top,
				bottom: dr.bottom,
				height: dr.height,
				width: dr.width,
				cx: dr.x + dr.width / 2,
				contentTop: dr.top + padTop,
				contentBottom: dr.bottom - padBottom
			},
			card: {
				top: cr.top,
				bottom: cr.bottom,
				height: cr.height,
				width: cr.width,
				cx: cr.x + cr.width / 2,
				cy: cr.top + cr.height / 2
			},
			panel: { height: pr.height },
			composer: { height: mr.height, bottom: mr.bottom, cx: mr.x + mr.width / 2 }
		};
	});
}

test.describe('hero state (no session)', () => {
	test('renders a COMPACT centered card hugging ONLY the composer — no full-height slab', async ({
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

		// CHC H1: the blended CARD is COMPACT — its height is the composer
		// block's height (panel + card border), NOT the host height — and
		// it is CENTERED in the stage (equal space above and below), with
		// the input at the card's bottom edge.
		const geo = await cardGeometry(page);
		expect(Math.abs(geo.card.height - geo.composer.height)).toBeLessThanOrEqual(4);
		expect(geo.card.height).toBeLessThan(geo.dock.height / 2);
		expect(geo.card.height).toBeLessThan(320);
		expect(
			Math.abs(geo.card.top - geo.dock.contentTop - (geo.dock.contentBottom - geo.card.bottom)),
			'card vertically centered in the column'
		).toBeLessThanOrEqual(1.5);
		expect(Math.abs(geo.card.cx - geo.dock.cx)).toBeLessThanOrEqual(1);
		expect(geo.card.top).toBeGreaterThan(geo.dock.top + 100);
		expect(Math.abs(geo.composer.bottom - (geo.card.bottom - 1))).toBeLessThanOrEqual(2);
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
	test('first submit GROWS the card to full height without remounting the composer', async ({
		page
	}) => {
		await gotoHarness(page);
		const input = page.getByTestId('session-composer-input');
		await expect(input).toBeVisible();
		const heroGeo = await cardGeometry(page);
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

		// CHC H1: the GROWTH is a real WAAPI card animation (~240ms ease)
		// whose height is INTERMEDIATE right after the flip — strictly
		// between the compact hero height and the full column height.
		// Two rAFs land ~1-2 frames into the 240ms window, before any
		// assertion polling could eat the animation.
		const growth = await page.evaluate(async () => {
			const card = document.querySelector('.chc-dock__card')!;
			const firstFrame = Promise.withResolvers<void>();
			requestAnimationFrame(() => firstFrame.resolve());
			await firstFrame.promise;
			const secondFrame = Promise.withResolvers<number>();
			requestAnimationFrame(() =>
				secondFrame.resolve(card.getBoundingClientRect().height)
			);
			const height = await secondFrame.promise;
			return {
				height,
				timings: card
					.getAnimations()
					.map((animation) => animation.effect?.getTiming() ?? null)
			};
		});
		expect(growth.timings.length).toBeGreaterThanOrEqual(1);
		expect(growth.timings[0]?.duration).toBe(240);
		expect(growth.timings[0]?.easing).toBe('ease');
		expect(growth.height).toBeGreaterThan(heroGeo.card.height + 4);
		expect(growth.height).toBeLessThan(heroGeo.dock.height - 200);

		// Same DOM node (marked), still holding focus; the draft was
		// cleared by the submit exactly as before.
		const survivor = page.getByTestId('session-composer-input');
		await expect(survivor).toHaveAttribute('data-hero-mark', 'survivor');
		await expect(survivor).toHaveValue('');
		const focused = await page.evaluate(
			() => document.activeElement?.getAttribute('data-testid') ?? null
		);
		expect(focused).toBe('session-composer-input');

		// After the growth settles (~240ms + margin): the card fills the
		// column exactly, the viewport owns the freed height, and the
		// composer sits at the TRUE bottom edge of the full card.
		await page.waitForTimeout(450);
		const settled = await page.evaluate(() => {
			const cardEl = document.querySelector('.chc-dock__card')!;
			const dockEl = document.querySelector('.chc-dock')!;
			const viewportEl = document.querySelector('[data-testid="session-history-viewport"]')!;
			const composerEl = document.querySelector('.mai-session-chat__composer')!;
			const cr = cardEl.getBoundingClientRect();
			const dr = dockEl.getBoundingClientRect();
			const dc = getComputedStyle(dockEl);
			return {
				cardHeight: cr.height,
				cardBottom: cr.bottom,
				columnHeight:
					dr.height - parseFloat(dc.paddingTop) - parseFloat(dc.paddingBottom),
				viewportHeight: viewportEl.getBoundingClientRect().height,
				composerBottom: composerEl.getBoundingClientRect().bottom,
				runningAnimations: cardEl.getAnimations().length
			};
		});
		expect(settled.runningAnimations).toBe(0);
		expect(Math.abs(settled.cardHeight - settled.columnHeight)).toBeLessThanOrEqual(2);
		expect(settled.cardHeight).toBeGreaterThan(512);
		expect(settled.viewportHeight).toBeGreaterThan(400);
		expect(Math.abs(settled.composerBottom - settled.cardBottom)).toBeLessThanOrEqual(2);
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

		// CHC H1: a loaded session renders ACTIVE FULL-HEIGHT directly —
		// the card already fills the column with the composer at the
		// bottom; no growth animation runs on load (no hero flash).
		const loaded = await page.evaluate(() => {
			const cardEl = document.querySelector('.chc-dock__card')!;
			const dockEl = document.querySelector('.chc-dock')!;
			const viewportEl = document.querySelector('[data-testid="session-history-viewport"]')!;
			const composerEl = document.querySelector('.mai-session-chat__composer')!;
			const cr = cardEl.getBoundingClientRect();
			const dr = dockEl.getBoundingClientRect();
			const dc = getComputedStyle(dockEl);
			return {
				cardHeight: cr.height,
				cardBottom: cr.bottom,
				columnHeight:
					dr.height - parseFloat(dc.paddingTop) - parseFloat(dc.paddingBottom),
				viewportHeight: viewportEl.getBoundingClientRect().height,
				composerBottom: composerEl.getBoundingClientRect().bottom,
				animations: cardEl.getAnimations().length
			};
		});
		expect(loaded.animations).toBe(0);
		expect(Math.abs(loaded.cardHeight - loaded.columnHeight)).toBeLessThanOrEqual(2);
		expect(loaded.cardHeight).toBeGreaterThan(512);
		expect(loaded.viewportHeight).toBeGreaterThan(400);
		expect(Math.abs(loaded.composerBottom - loaded.cardBottom)).toBeLessThanOrEqual(2);
	});
});

test.describe('motion honesty', () => {
	test('the ~240ms card growth runs by default and is INSTANT under reduced motion', async ({
		page
	}) => {
		// Default: the growth is the WAAPI animation proven mid-flight by
		// the transition suite; under reduce the flip must be INSTANT —
		// no Web Animations on the card and no CSS transition/animation
		// anywhere in the harness (tokens.css forces 0.01ms !important
		// durations under reduce; anything under 1ms IS "no transition"
		// for an observer. Durations normalize ms → s.).
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await gotoHarness(page);
		await expect(page.getByTestId('session-composer-input')).toBeVisible();
		await page.getByTestId('session-composer-input').click();
		await page.keyboard.type('quiet launch');
		await page.keyboard.press('Enter');
		await expect(page.getByTestId('session-chat-panel')).toHaveAttribute(
			'data-state',
			'active'
		);
		const reduced = await page.evaluate(async () => {
			const cardEl = document.querySelector('.chc-dock__card')!;
			const dockEl = document.querySelector('.chc-dock')!;
			const frame = Promise.withResolvers<void>();
			requestAnimationFrame(() => frame.resolve());
			await frame.promise;
			const cr = cardEl.getBoundingClientRect();
			const dr = dockEl.getBoundingClientRect();
			const dc = getComputedStyle(dockEl);
			const root = document.getElementById('app')!;
			const animated = [...root.querySelectorAll('*'), root]
				.filter((el) => {
					const cs = getComputedStyle(el);
					const raw = cs.transitionDuration;
					return (
						parseFloat(raw) * (raw.includes('ms') ? 0.001 : 1) >= 0.001 ||
						cs.animationName !== 'none'
					);
				})
				.map((el) => el.getAttribute('class') ?? el.tagName);
			return {
				cardHeight: cr.height,
				columnHeight:
					dr.height - parseFloat(dc.paddingTop) - parseFloat(dc.paddingBottom),
				cardAnimations: cardEl.getAnimations().length,
				animated
			};
		});
		expect(reduced.cardAnimations).toBe(0);
		expect(Math.abs(reduced.cardHeight - reduced.columnHeight)).toBeLessThanOrEqual(2);
		expect(reduced.animated).toEqual([]);
	});
});

test.describe('360x640 bounds', () => {
	test('the compact hero card fits without overflow', async ({ page }) => {
		await page.setViewportSize({ width: 360, height: 640 });
		await gotoHarness(page);
		await expect(page.getByTestId('session-composer-input')).toBeVisible();
		const overflow = await page.evaluate(() => {
			const input = document
				.querySelector('[data-testid="session-composer-input"]')!
				.getBoundingClientRect();
			const card = document.querySelector('.chc-dock__card')!.getBoundingClientRect();
			return {
				scroll: document.documentElement.scrollWidth,
				client: document.documentElement.clientWidth,
				inputRight: input.right,
				inputBottom: input.bottom,
				cardBottom: card.bottom,
				cardHeight: card.height
			};
		});
		expect(overflow.scroll).toBeLessThanOrEqual(overflow.client);
		expect(overflow.inputRight).toBeLessThanOrEqual(360);
		expect(overflow.inputBottom).toBeLessThanOrEqual(640);
		expect(overflow.cardBottom).toBeLessThanOrEqual(640);
		expect(overflow.cardHeight).toBeLessThan(320);
	});
});
