import { expect, test } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * history-tags.spec.ts — real-browser proofs for HistoryTags.svelte
 * (history-tags W7A, owned files: HistoryTags.svelte + this spec).
 *
 * Harness: in-memory Vite production build (write:false), same approach as
 * details-disclosure.spec.ts. Compiled Svelte 5 runs in real Chromium so
 * hover/blur/focus semantics, ARIA wiring, touch taps, pagination honesty,
 * and viewport clamping are exercised as the browser implements them.
 *
 * Scenarios owned here:
 *  1. preview — compact truncated chips, honest "+N" only when total > preview
 *  2. initially closed — dialog hidden, aria-expanded=false, aria-controls wired
 *  3. hover opens; mouse leave keeps open; click toggles closed; hover reopens
 *  4. keyboard — focus opens, blur to sibling closes, Enter/Space toggle
 *  5. Escape closes from inside the popup and restores trigger focus
 *  6. outside pointerdown closes
 *  7. load more — PAGE_LIMIT(10) pages, dedup by tag_id, has_more honesty
 *  8. first page pending — honest loading state, then rows
 *  9. fetch failure — honest error state with working retry
 * 10. empty page — honest empty state
 * 11. no loaders — honest preview-only popup, no load control
 * 12. supplied firstPage adopted instantly without an adapter call
 * 13. sibling instances independent — no shared module state
 * 14. 360×640 — no document horizontal overflow, popup clamped/flipped,
 *     bounded scroll region, truncated chips
 * 15. touch tap toggles open and closed (hasTouch context)
 */

const FIXTURE_URL = '/mai-history-tags-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const VIRTUAL_ID = 'virtual:mai-history-tags-entry';

const HISTORY_TAGS = `${FRONTEND_ROOT}src/lib/ui/HistoryTags.svelte`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

declare global {
	interface Window {
		__maiTagsState?: {
			calls: Array<{ instance: string; offset: number; limit: number }>;
			failCount: number;
		};
		__resolveSlow?: () => void;
	}
}

let harnessJs = '';
let harnessCss = '';

function entrySource(): string {
	return `
import { mount, createRawSnippet } from 'svelte';
import HistoryTags from ${JSON.stringify(HISTORY_TAGS)};
import ComponentHost from ${JSON.stringify(HOST)};
import ${JSON.stringify(TOKENS)};

const state = { calls: [], failCount: 0 };
window.__maiTagsState = state;

const mkTag = (i) => ({
  tag_id: '00000000-0000-4000-8000-' + String(i).padStart(12, '0'),
  name: 'tag-' + String(i).padStart(2, '0'),
  assigned_by: '',
  assignment_source: 'system',
  created_at: '2026-01-01T00:00:00Z',
});

const mkPage = (offset, items, total, hasMore) => ({
  limit: 10, offset, items, total, has_more: hasMore,
});

const preview = (names, total) => ({ preview: names, total });

// a — 25 wire tags across three pages; page two re-serves tag-09 so the
// client-side dedup by tag_id is provable (19 unique after page two).
const aTags = Array.from({ length: 25 }, (_, i) => mkTag(i));
const adapterA = async (offset, limit) => {
  state.calls.push({ instance: 'a', offset, limit });
  if (offset === 0) return mkPage(0, aTags.slice(0, 10), 25, true);
  if (offset === 10) return mkPage(10, aTags.slice(10, 20), 25, true);
  // Final page re-serves tag-09 as a stray duplicate row: the client must
  // dedup it and still land on an honest 25/25 with Load more hidden.
  return mkPage(20, aTags.slice(20, 25).concat([aTags[9]]), 25, false);
};

// slow — first page hangs until the test resolves it (loading-state proof).
const slowGate = Promise.withResolvers();
const adapterSlow = (offset, limit) => {
  state.calls.push({ instance: 'slow', offset, limit });
  return slowGate.promise;
};
window.__resolveSlow = () => slowGate.resolve(mkPage(0, [mkTag(0)], 1, false));

// fail — first attempt rejects, retry succeeds (error/retry proof).
const adapterFail = async (offset, limit) => {
  state.calls.push({ instance: 'fail', offset, limit });
  state.failCount += 1;
  if (state.failCount === 1) throw new Error('boom');
  return mkPage(0, [mkTag(0), mkTag(1)], 2, false);
};

// empty — honest zero-row page.
const adapterEmpty = async (offset, limit) => {
  state.calls.push({ instance: 'empty', offset, limit });
  return mkPage(0, [], 0, false);
};

// wide — long names for truncation and viewport-clamping proofs.
const LONG = 'infra-observability-retention-sweep-lakehouse';
const wideTags = Array.from({ length: 7 }, (_, i) => ({
  ...mkTag(100 + i),
  name: LONG + '-' + i,
}));
const adapterWide = async (offset, limit) => {
  state.calls.push({ instance: 'wide', offset, limit });
  return mkPage(0, wideTags, 7, false);
};

const instances = [
  { id: 'a', props: { tag_preview: preview(['tag-00', 'tag-01', 'tag-02'], 25), loadMore: adapterA, label: 'Conversation a' } },
  { id: 'slow', props: { tag_preview: preview(['slow-tag'], 1), loadMore: adapterSlow } },
  { id: 'fail', props: { tag_preview: preview(['fail-tag'], 2), loadMore: adapterFail } },
  { id: 'empty', props: { tag_preview: preview(['phantom'], 1), loadMore: adapterEmpty } },
  { id: 'bare', props: { tag_preview: preview(['ops'], 1) } },
  { id: 'firstpage', props: { tag_preview: preview(['tag-00', 'tag-01'], 10), firstPage: mkPage(0, Array.from({ length: 10 }, (_, i) => mkTag(i)), 10, false) } },
  { id: 'wide', props: { tag_preview: preview([LONG + '-0', LONG + '-1', LONG + '-2'], 7), loadMore: adapterWide } },
  { id: 'edge', props: { tag_preview: preview(['edge-tag'], 1) } },
];

const app = document.getElementById('app');
for (const inst of instances) {
  const wrapper = document.createElement('div');
  wrapper.setAttribute('data-testid', 'instance-' + inst.id);
  wrapper.style.marginBottom = '8px';
  if (inst.id === 'edge') {
    // Push the root near the right viewport edge so the popup flip logic
    // must right-align it to stay inside a 360px viewport.
    wrapper.style.marginLeft = 'calc(100vw - 128px)';
    wrapper.style.width = '120px';
  }
  app.appendChild(wrapper);
  mount(HistoryTags, { target: wrapper, props: { testId: 'tags-' + inst.id, ...inst.props } });
}

// One instance inside the component-host to prove isolation through a host boundary.
mount(ComponentHost, {
  target: app,
  props: {
    label: 'MAI history-tags host',
    children: createRawSnippet(() => ({
      render: () => '<div data-testid="hosted-container"></div>',
    })),
  },
});

const hostedContainer = document.querySelector('[data-testid="hosted-container"]');
mount(HistoryTags, {
  target: hostedContainer,
  props: { testId: 'tags-hosted', tag_preview: preview(['ops'], 1) },
});
`;
}

function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-history-tags-harness',
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
			outDir: 'node_modules/.mai-history-tags-fixture',
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
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete: missing JS chunk or CSS asset');
	}
});

test.beforeEach(async ({ page }) => {
	// Park the virtual mouse on empty canvas BEFORE the fixture loads: the
	// mouse position survives page.goto(), and a pointer left resting over
	// the top-left instance's trigger races Chromium's synthetic boundary
	// events after navigation (self-opened popups then overlay other
	// instances' triggers). (200, 560) is below the rendered column in both
	// the default and the 360×640 viewports.
	await page.mouse.move(200, 560);

	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAI HistoryTags fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body style="padding-top:24px">
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

	// Make the origin deterministic on the live document: Chromium's
	// post-navigation pointer burst replays the browser's cached position,
	// and a pre-goto park can lose that race (burst then lands on the
	// origin instance's trigger at (0,0) and self-opens it — observed in
	// the popup-a1 run). Re-sending the move fixes the cached position on
	// the new document; the outside pointerdown closes anything a burst
	// that already fired may have opened, so every scenario starts with
	// every popup honestly closed.
	await page.mouse.move(200, 560);
	await page.mouse.down();
	await page.mouse.up();
	// The fixture column is offset 24px below the origin (body padding), so
	// Chromium's post-navigation pointer burst — whatever stale position it
	// replays — cannot land on a trigger; the pointerdown above is defense
	// in depth. No popup can be open or warmed here: scenarios start from a
	// component that has neither fetched nor rendered a single row.
});

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

test('preview — compact truncated chips with honest +N', async ({ page }) => {
	// Instance a: 3 preview names, total 25 → +22.
	const aTrigger = page.getByTestId('tags-a-trigger');
	await expect(aTrigger).toBeVisible();
	const aChips = page.getByTestId('tags-a-chip');
	await expect(aChips).toHaveCount(3);
	await expect(aChips.filter({ hasText: 'tag-00' })).toHaveCount(1);
	await expect(page.getByTestId('tags-a-more')).toHaveText('+22');
	await expect(page.getByTestId('tags-a-more')).toHaveAttribute(
		'title',
		'22 more tags'
	);

	// Instance bare: total 1 == preview 1 → no +N.
	await expect(page.getByTestId('tags-bare-chip')).toHaveCount(1);
	await expect(page.getByTestId('tags-bare-more')).toHaveCount(0);
});

test('initially closed — dialog hidden, aria-expanded=false, aria-controls wired', async ({
	page
}) => {
	for (const id of ['a', 'slow', 'fail', 'empty', 'bare', 'firstpage', 'wide', 'edge', 'hosted']) {
		const trigger = page.getByTestId(`tags-${id}-trigger`);
		const pop = page.getByTestId(`tags-${id}-pop`);

		await expect(trigger).toBeVisible();
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
		await expect(trigger).toHaveAttribute('aria-haspopup', 'dialog');
		// aria-controls must resolve to the live dialog element's id.
		const controls = await trigger.getAttribute('aria-controls');
		expect(controls).toBeTruthy();
		await expect(pop).toHaveAttribute('id', controls!);
		await expect(pop).toBeHidden();
	}
});

test('origin load — synthetic post-navigation hover cannot open the trigger', async ({
	page
}) => {
	// Chromium replays a trusted pointerover → pointerenter → pointermove
	// burst after navigation, with no user input, at whatever position the
	// browser last had (origin (0,0) for a fresh page). No component can
	// distinguish that trusted synthetic pointermove from a real one, so the
	// harness owns origin determinism: the fixture column is offset below
	// (0,0) and the pointer is re-parked off-widget, making the burst unable
	// to reach any trigger. The scenario proves the outcome: after load with
	// zero scenario input, the trigger is closed and no adapter ran.
	await page.waitForTimeout(250);

	const trigger = page.getByTestId('tags-a-trigger');
	await expect(trigger).toHaveAttribute('aria-expanded', 'false');
	await expect(page.getByTestId('tags-a-pop')).toBeHidden();
	expect(await page.evaluate(() => window.__maiTagsState?.calls)).toEqual([]);
});

test('hover opens; mouse leave keeps open; click toggles closed; hover reopens', async ({
	page
}) => {
	const trigger = page.getByTestId('tags-a-trigger');
	const pop = page.getByTestId('tags-a-pop');

	await trigger.hover();
	await expect(pop).toBeVisible();
	await expect(trigger).toHaveAttribute('aria-expanded', 'true');

	// Moving the mouse away must not close the popup.
	await page.mouse.move(5, 5);
	await expect(pop).toBeVisible();

	// Click toggles: the pointer is already over the trigger, so the click
	// closes it and hover stays suppressed until the pointer leaves.
	await trigger.click();
	await expect(pop).toBeHidden();
	await expect(trigger).toHaveAttribute('aria-expanded', 'false');

	// Reopen requires a genuine re-entry: move away (pointerleave clears the
	// click-close suppression), then hover back.
	await page.mouse.move(320, 420);
	await trigger.hover();
	await expect(pop).toBeVisible();
});

test('keyboard — focus opens, blur to sibling closes, Enter/Space toggle', async ({ page }) => {
	const aTrigger = page.getByTestId('tags-a-trigger');
	const firstpageTrigger = page.getByTestId('tags-firstpage-trigger');

	await aTrigger.focus();
	await expect(aTrigger).toBeFocused();
	await expect(page.getByTestId('tags-a-pop')).toBeVisible();
	await expect(aTrigger).toHaveAttribute('aria-expanded', 'true');

	// The popup list is keyboard-operable, not hover-only: Tab from the
	// trigger lands on the first tag row (BRIEF acceptance criterion 9).
	await expect(page.getByTestId('tags-a-row').first()).toBeVisible();
	await page.keyboard.press('Tab');
	await expect(page.getByTestId('tags-a-row').first()).toBeFocused();

	// Blur: focusing the sibling closes a (and opens the sibling — independence).
	await firstpageTrigger.focus();
	await expect(page.getByTestId('tags-a-pop')).toBeHidden();
	await expect(page.getByTestId('tags-firstpage-pop')).toBeVisible();

	// Native button: Enter toggles the focus-opened popup closed, Space reopens.
	await page.keyboard.press('Enter');
	await expect(page.getByTestId('tags-firstpage-pop')).toBeHidden();
	await page.keyboard.press(' ');
	await expect(page.getByTestId('tags-firstpage-pop')).toBeVisible();
});

test('Escape closes from inside the popup and restores trigger focus', async ({ page }) => {
	const trigger = page.getByTestId('tags-a-trigger');
	const pop = page.getByTestId('tags-a-pop');

	await trigger.hover();
	const loadMore = page.getByTestId('tags-a-load-more');
	await loadMore.click();
	await expect(loadMore).toBeFocused();

	await page.keyboard.press('Escape');
	await expect(pop).toBeHidden();
	await expect(trigger).toHaveAttribute('aria-expanded', 'false');
	await expect(trigger).toBeFocused();
});

test('outside pointerdown closes', async ({ page }) => {
	const trigger = page.getByTestId('tags-a-trigger');
	const pop = page.getByTestId('tags-a-pop');

	await trigger.hover();
	await expect(pop).toBeVisible();

	await page.mouse.click(400, 560);
	await expect(pop).toBeHidden();
	await expect(trigger).toHaveAttribute('aria-expanded', 'false');
});

test('load more — PAGE_LIMIT pages, dedup by tag_id, has_more honesty', async ({ page }) => {
	const trigger = page.getByTestId('tags-a-trigger');
	const pop = page.getByTestId('tags-a-pop');
	const rows = page.getByTestId('tags-a-row');
	const count = page.getByTestId('tags-a-count');
	const loadMore = page.getByTestId('tags-a-load-more');

	await trigger.hover();

	// First page: bounded at PAGE_LIMIT=10, offset 0.
	await expect(rows).toHaveCount(10);
	await expect(count).toHaveText('Showing 10 of 25');
	await expect(loadMore).toBeVisible();
	expect(await page.evaluate(() => window.__maiTagsState?.calls)).toEqual([
		{ instance: 'a', offset: 0, limit: 10 }
	]);

	// Second page: honest slots t10..t19 → 20 unique rows.
	await loadMore.click();
	await expect(rows).toHaveCount(20);
	await expect(count).toHaveText('Showing 20 of 25');
	await expect(loadMore).toBeVisible();
	expect(await page.evaluate(() => window.__maiTagsState?.calls)).toEqual([
		{ instance: 'a', offset: 0, limit: 10 },
		{ instance: 'a', offset: 10, limit: 10 }
	]);

	// Third page re-serves tag-09 as a stray duplicate: dedup keeps exactly
	// 25 unique rows and has_more=false hides Load more.
	await loadMore.click();
	await expect(rows).toHaveCount(25);
	await expect(count).toHaveText('Showing 25 of 25');
	await expect(loadMore).toHaveCount(0);
	expect(await page.evaluate(() => window.__maiTagsState?.calls)).toEqual([
		{ instance: 'a', offset: 0, limit: 10 },
		{ instance: 'a', offset: 10, limit: 10 },
		{ instance: 'a', offset: 20, limit: 10 }
	]);

	// Every rendered row is unique by construction of the dedup proof.
	const names = await rows.allTextContents();
	expect(new Set(names).size).toBe(25);
});

test('first page pending — honest loading state, then rows', async ({ page }) => {
	const pop = page.getByTestId('tags-slow-pop');
	await page.getByTestId('tags-slow-trigger').hover();

	const loading = page.getByTestId('tags-slow-loading');
	await expect(loading).toBeVisible();
	await expect(loading).toHaveText('Loading tags…');
	await expect(pop).toHaveAttribute('aria-busy', 'true');
	await expect(page.getByTestId('tags-slow-row')).toHaveCount(0);

	await page.evaluate(() => window.__resolveSlow?.());
	await expect(page.getByTestId('tags-slow-row')).toHaveCount(1);
	await expect(page.getByTestId('tags-slow-loading')).toHaveCount(0);
	await expect(page.getByTestId('tags-slow-count')).toHaveText('Showing 1 of 1');
	await expect(pop).toHaveAttribute('aria-busy', 'false');
});

test('fetch failure — honest error state with working retry', async ({ page }) => {
	await page.getByTestId('tags-fail-trigger').hover();

	const error = page.getByTestId('tags-fail-error');
	await expect(error).toBeVisible();
	await expect(error).toHaveText('Tag list failed to load.');
	await expect(page.getByTestId('tags-fail-row')).toHaveCount(0);

	await page.getByTestId('tags-fail-retry').click();
	await expect(page.getByTestId('tags-fail-row')).toHaveCount(2);
	await expect(error).toHaveCount(0);
	await expect(page.getByTestId('tags-fail-count')).toHaveText('Showing 2 of 2');
});

test('empty page — honest empty state', async ({ page }) => {
	await page.getByTestId('tags-empty-trigger').hover();

	await expect(page.getByTestId('tags-empty-empty')).toHaveText('No tags attached.');
	await expect(page.getByTestId('tags-empty-row')).toHaveCount(0);
	await expect(page.getByTestId('tags-empty-count')).toHaveText('Showing 0 of 0');
	await expect(page.getByTestId('tags-empty-load-more')).toHaveCount(0);
});

test('no loaders — honest preview-only popup, no load control', async ({ page }) => {
	await page.getByTestId('tags-bare-trigger').hover();

	await expect(page.getByTestId('tags-bare-empty')).toHaveText('Full tag list unavailable.');
	await expect(page.getByTestId('tags-bare-row')).toHaveCount(0);
	await expect(page.getByTestId('tags-bare-count')).toHaveCount(0);
	await expect(page.getByTestId('tags-bare-load-more')).toHaveCount(0);
});

test('supplied firstPage adopted instantly without an adapter call', async ({ page }) => {
	await page.getByTestId('tags-firstpage-trigger').hover();

	// Rows are present immediately (no loading state ever shown), and no
	// adapter was consulted — the supplied page is the first page.
	await expect(page.getByTestId('tags-firstpage-row')).toHaveCount(10);
	await expect(page.getByTestId('tags-firstpage-loading')).toHaveCount(0);
	await expect(page.getByTestId('tags-firstpage-count')).toHaveText('Showing 10 of 10');
	await expect(page.getByTestId('tags-firstpage-load-more')).toHaveCount(0);
	expect(await page.evaluate(() => window.__maiTagsState?.calls)).toEqual([]);
});

test('sibling instances are independent — opening one does not affect others', async ({
	page
}) => {
	await page.getByTestId('tags-a-trigger').hover();
	await expect(page.getByTestId('tags-a-pop')).toBeVisible();
	await expect(page.getByTestId('tags-firstpage-pop')).toBeHidden();
	await expect(page.getByTestId('tags-bare-pop')).toBeHidden();

	// aria-controls values are unique per instance (no shared module ids).
	const controls: string[] = [];
	for (const id of ['a', 'firstpage', 'bare', 'hosted']) {
		controls.push(
			(await page.getByTestId(`tags-${id}-trigger`).getAttribute('aria-controls'))!
		);
	}
	expect(new Set(controls).size).toBe(4);
});

test.describe('360×640 mobile viewport', () => {
	test.use({ viewport: { width: 360, height: 640 } });

	test('no document horizontal overflow; popup clamped; bounded scroll; chips truncate', async ({
		page
	}) => {
		// Long-name popup at the default (left-aligned) position.
		await page.getByTestId('tags-wide-trigger').hover();
		await expect(page.getByTestId('tags-wide-row')).toHaveCount(7);

		let popBox = (await page.getByTestId('tags-wide-pop').boundingBox())!;
		expect(popBox.x).toBeGreaterThanOrEqual(0);
		expect(popBox.x + popBox.width).toBeLessThanOrEqual(360);

		// Document never overflows horizontally while a popup is open.
		const scrollWidth = await page.evaluate(
			() => document.scrollingElement?.scrollWidth ?? 0
		);
		expect(scrollWidth).toBeLessThanOrEqual(360);

		// Preview chips truncate instead of pushing the trigger wide. The
		// clipping element is the chip's inner content span (the Chip root is
		// an inline-flex wrapper that stays at max-width while the content
		// span ellipsizes), so assert on the element that actually clips.
		const chip = page.getByTestId('tags-wide-chip').first();
		const truncated = await chip.evaluate((el) => {
			const clipper = el.querySelector('.mai-chip__content');
			if (!clipper) return false;
			const style = getComputedStyle(clipper);
			return (
				style.textOverflow === 'ellipsis' &&
				style.whiteSpace === 'nowrap' &&
				clipper.scrollWidth > clipper.clientWidth &&
				clipper.clientWidth > 0
			);
		});
		expect(truncated).toBe(true);

		// Near the right edge the popup flips to right-alignment and stays inside.
		await page.getByTestId('tags-edge-trigger').hover();
		await expect(page.getByTestId('tags-edge-empty')).toBeVisible();
		popBox = (await page.getByTestId('tags-edge-pop').boundingBox())!;
		expect(popBox.x).toBeGreaterThanOrEqual(0);
		expect(popBox.x + popBox.width).toBeLessThanOrEqual(360);
		expect(
			await page.evaluate(() => document.scrollingElement?.scrollWidth ?? 0)
		).toBeLessThanOrEqual(360);

		// Full list scroll region is height-bounded even with 25 rows loaded.
		// Open instance a explicitly (no cross-test mouse state) and page
		// through all three adapter pages.
		await page.getByTestId('tags-a-trigger').hover();
		await expect(page.getByTestId('tags-a-row')).toHaveCount(10);
		await page.getByTestId('tags-a-load-more').click();
		await expect(page.getByTestId('tags-a-row')).toHaveCount(20);
		await page.getByTestId('tags-a-load-more').click();
		await expect(page.getByTestId('tags-a-row')).toHaveCount(25);
		const listBounds = await page
			.getByTestId('tags-a-list')
			.evaluate((el) => ({
				clientHeight: el.clientHeight,
				scrollHeight: el.scrollHeight,
				maxHeight: parseInt(getComputedStyle(el).maxHeight, 10)
			}));
		expect(listBounds.clientHeight).toBeLessThanOrEqual(listBounds.maxHeight);
		expect(listBounds.scrollHeight).toBeGreaterThan(listBounds.clientHeight);
	});
});

test.describe('touch tap (hasTouch)', () => {
	test.use({ hasTouch: true });

	test('tap toggles open and closed', async ({ page }) => {
		const trigger = page.getByTestId('tags-a-trigger');
		const pop = page.getByTestId('tags-a-pop');
		const box = (await trigger.boundingBox())!;

		await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
		await expect(pop).toBeVisible();
		await expect(trigger).toHaveAttribute('aria-expanded', 'true');

		await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
		await expect(pop).toBeHidden();
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
	});
});
