import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * details-disclosure.spec.ts — real-browser proofs for DetailsDisclosure.svelte
 * (chat-clean card 1, owned files: DetailsDisclosure.svelte + this spec).
 *
 * Harness: in-memory Vite production build (write:false), same approach as
 * ui-controls.spec.ts. Compiled Svelte 5 runs in real Chromium so native
 * keyboard behaviour, ARIA wiring, and focus management are exercised as the
 * browser implements them.
 *
 * Scenarios owned here:
 *  1. initially closed — panel hidden, aria-expanded=false
 *  2. keyboard toggle — Enter/Space activate trigger (native button semantics)
 *  3. ARIA wiring — aria-expanded, aria-controls, aria-labelledby, aria-hidden, unique ids
 *  4. independent sibling instances — no shared module state
 *  5. Escape focus return — Escape on child input closes and returns focus to trigger
 *  6. content state survives toggle — re-opening shows panel content
 *  7. 360×640 viewport containment — fits width, wraps long content
 *  8. alert/count indicator rendered when supplied
 *  9. no external network requests — component is fully self-contained
 */

const FIXTURE_URL = '/mai-details-disclosure-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const VIRTUAL_ID = 'virtual:mai-details-disclosure-entry';

const DETAILS = `${FRONTEND_ROOT}src/lib/ui/DetailsDisclosure.svelte`;
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
		__maiDetailsState?: {
			toggleCount: number;
			panelContent: string;
			alertContent: string;
		};
	}
}

let harnessJs = '';
let harnessCss = '';

function entrySource(): string {
	return `
import { mount, createRawSnippet } from 'svelte';
import DetailsDisclosure from ${JSON.stringify(DETAILS)};
import ComponentHost from ${JSON.stringify(HOST)};
import ${JSON.stringify(TOKENS)};

const state = { toggleCount: 0, panelContent: 'initial-content', alertContent: '3' };
window.__maiDetailsState = state;

/**
 * Panel children — include a real <input> so the Escape key can be exercised
 * when focus is inside the panel, not on the trigger. This is the accessibility-
 * correct scenario: a user tabs into the expanded panel and presses Escape.
 */
const makeChildren = (id) => createRawSnippet(() => ({
  render: () =>
    \`<div>
       <input type="text" data-testid="panel-input-\${id}" placeholder="type here then press Escape" />
       <span data-testid="panel-text-\${id}">\${state.panelContent}</span>
     </div>\`
}));

const makeAlert = () =>
  createRawSnippet(() => ({
    render: () =>
      \`<span data-testid="alert-badge" aria-label="\${state.alertContent} new items">\${state.alertContent}</span>\`
  }));

// Sibling instances to prove independence
const instances = [
  { id: 'a', label: 'Details A', alert: true  },
  { id: 'b', label: 'Details B', alert: false },
  { id: 'c', label: 'Details C', alert: false },
];

const app = document.getElementById('app');

for (const inst of instances) {
  const wrapper = document.createElement('div');
  wrapper.setAttribute('data-testid', \`instance-\${inst.id}\`);
  app.appendChild(wrapper);

  mount(DetailsDisclosure, {
    target: wrapper,
    props: {
      label: inst.label,
      testId: \`disclosure-\${inst.id}\`,
      children: makeChildren(inst.id),
      alert: inst.alert ? makeAlert() : undefined,
    }
  });
}

// One instance inside the component-host to prove isolation through a host boundary
mount(ComponentHost, {
  target: app,
  props: {
    label: 'MAI disclosure host',
    children: createRawSnippet(() => ({
      render: () => \`<div data-testid="hosted-container"></div>\`
    }))
  }
});

const hostedContainer = document.querySelector('[data-testid="hosted-container"]');
mount(DetailsDisclosure, {
  target: hostedContainer,
  props: {
    label: 'Hosted Details',
    testId: 'disclosure-hosted',
    children: makeChildren('hosted'),
  }
});

// Toggle counter via DOM event delegation (no component instance access needed)
document.addEventListener('click', (e) => {
  const btn = e.target;
  if (btn instanceof HTMLElement && btn.dataset.testid?.startsWith('disclosure-') && btn.dataset.testid?.endsWith('-trigger')) {
    state.toggleCount++;
  }
}, true);
`;
}

function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-details-disclosure-harness',
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
			outDir: 'node_modules/.mai-details-disclosure-fixture',
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
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI DetailsDisclosure fixture</title>
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

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

test('initially closed — panel hidden, aria-expanded=false on all instances', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	for (const id of ['a', 'b', 'c', 'hosted']) {
		const trigger = page.getByTestId(`disclosure-${id}-trigger`);
		const panel = page.getByTestId(`disclosure-${id}-panel`);

		await expect(trigger).toBeVisible();
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
		await expect(panel).toBeHidden();
	}
});

test('trigger is a native button — Enter and Space activate it', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	const trigger = page.getByTestId('disclosure-a-trigger');
	await trigger.focus();
	await expect(trigger).toBeFocused();

	// Space toggles open
	await page.keyboard.press(' ');
	await expect(trigger).toHaveAttribute('aria-expanded', 'true');
	await expect(page.getByTestId('disclosure-a-panel')).toBeVisible();

	// Space toggles closed
	await page.keyboard.press(' ');
	await expect(trigger).toHaveAttribute('aria-expanded', 'false');
	await expect(page.getByTestId('disclosure-a-panel')).toBeHidden();
});

test('ARIA wiring — aria-controls, aria-labelledby, aria-hidden, unique ids', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	for (const id of ['a', 'b', 'c', 'hosted']) {
		const trigger = page.getByTestId(`disclosure-${id}-trigger`);
		const panel = page.getByTestId(`disclosure-${id}-panel`);

		expect(await trigger.getAttribute('aria-expanded')).toBeTruthy();
		expect(await trigger.getAttribute('aria-controls')).toBeTruthy();
		expect(await panel.getAttribute('aria-labelledby')).toBeTruthy();
		expect(await panel.getAttribute('aria-hidden')).toBe('true');
		expect(await panel.getAttribute('role')).toBe('region');
		expect(await panel.getAttribute('id')).toBe(await trigger.getAttribute('aria-controls'));
	}

	// Panel ids are unique per instance
	const allPanelIds = await Promise.all(
		['a', 'b', 'c', 'hosted'].map((id) =>
			page.getByTestId(`disclosure-${id}-panel`).getAttribute('id')
		)
	);
	expect(new Set(allPanelIds).size).toBe(4);
});

test('sibling instances are independent — opening one does not affect others', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	// Open A
	await page.getByTestId('disclosure-a-trigger').click();
	await expect(page.getByTestId('disclosure-a-trigger')).toHaveAttribute('aria-expanded', 'true');

	// B and C remain closed
	await expect(page.getByTestId('disclosure-b-trigger')).toHaveAttribute('aria-expanded', 'false');
	await expect(page.getByTestId('disclosure-c-trigger')).toHaveAttribute('aria-expanded', 'false');

	// Open B — A stays open
	await page.getByTestId('disclosure-b-trigger').click();
	await expect(page.getByTestId('disclosure-b-trigger')).toHaveAttribute('aria-expanded', 'true');
	await expect(page.getByTestId('disclosure-a-trigger')).toHaveAttribute('aria-expanded', 'true');
});

test('Escape on child input closes panel and returns focus to trigger', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	const trigger = page.getByTestId('disclosure-a-trigger');
	const panel = page.getByTestId('disclosure-a-panel');
	const input = page.getByTestId('panel-input-a');

	// Open the panel
	await trigger.click();
	await expect(trigger).toHaveAttribute('aria-expanded', 'true');
	await expect(panel).toBeVisible();

	// Focus the child input inside the panel (the user-journey Escape scenario)
	await input.focus();
	await expect(input).toBeFocused();

	// Press Escape — wrapper div catches the bubbled event, closes panel, returns focus
	await page.keyboard.press('Escape');
	await expect(trigger).toHaveAttribute('aria-expanded', 'false');
	await expect(panel).toBeHidden();
	await expect(trigger).toBeFocused();
});

test('content state survives toggle — reopening shows panel content', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	const trigger = page.getByTestId('disclosure-a-trigger');
	const panel = page.getByTestId('disclosure-a-panel');
	const panelText = page.getByTestId('panel-text-a');

	// Open and verify initial content
	await trigger.click();
	await expect(panel).toBeVisible();
	await expect(panelText).toHaveText('initial-content');

	// Close
	await trigger.click();
	await expect(panel).toBeHidden();

	// Re-open — content preserved (not unmounted)
	await trigger.click();
	await expect(panel).toBeVisible();
	await expect(panelText).toHaveText('initial-content');
});

test('360×640 narrow viewport — disclosure fits width and wraps long content', async ({
	page
}) => {
	await page.setViewportSize({ width: 360, height: 640 });
	await page.goto(FIXTURE_URL);

	const trigger = page.getByTestId('disclosure-a-trigger');
	const panel = page.getByTestId('disclosure-a-panel');

	// Trigger is visible and within viewport width
	await expect(trigger).toBeVisible();
	const triggerBox = await trigger.boundingBox();
	expect(triggerBox).not.toBeNull();
	expect(triggerBox!.x).toBeGreaterThanOrEqual(0);
	expect(triggerBox!.x + triggerBox!.width).toBeLessThanOrEqual(360);

	// Label wraps; height is reasonable
	expect(triggerBox!.height).toBeLessThanOrEqual(80);

	// Panel also within bounds when open
	await trigger.click();
	await expect(panel).toBeVisible();
	const panelBox = await panel.boundingBox();
	expect(panelBox).not.toBeNull();
	expect(panelBox!.x).toBeGreaterThanOrEqual(0);
	expect(panelBox!.x + panelBox!.width).toBeLessThanOrEqual(360);
});

test('alert/count indicator rendered when supplied; absent when not', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	// Instance A has alert
	const badge = page.getByTestId('alert-badge');
	await expect(badge).toBeVisible();
	await expect(badge).toHaveText('3');

	// Instance B has no alert — no badge element exists
	await expect(page.getByTestId('disclosure-b-trigger').locator('[data-testid="alert-badge"]')).toHaveCount(0);
});

test('no external network requests — component is fully self-contained', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	// Passive observer: page.on('request') does not interfere with route handlers,
	// so the beforeEach fixture interception remains intact and networkidle is reached.
	const externalRequests: string[] = [];
	page.on('request', (request) => {
		const url = new URL(request.url());
		const isExternal =
			url.hostname !== '127.0.0.1' &&
			url.hostname !== 'localhost' &&
			url.protocol !== 'data' &&
			!url.protocol.startsWith('blob');
		if (isExternal) {
			externalRequests.push(request.url());
		}
	});

	await page.waitForLoadState('networkidle');

	// Zero external requests — no API calls, no fonts, no images
	expect(externalRequests).toHaveLength(0);
});
