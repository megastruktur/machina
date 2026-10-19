import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-ui-controls — real-browser proofs for the shared button/field primitives
 * (frontend/src/lib/ui/Button.svelte, frontend/src/lib/ui/Field.svelte).
 *
 * Focused per the test policy: UI interaction primitives are exercised in the
 * real Chromium browser against the reviewed 01-web component fixture
 * (tests/fixtures/component-host.svelte) — no backend, no invented API.
 *
 * Harness (self-contained; the brief's allowlist covers only this spec and the
 * two components, so no fixture files or config edits are committed):
 *  1. test.beforeAll runs an IN-MEMORY Vite production build (write: false) of
 *     a virtual entry that imports the two components, the token layer and the
 *     reviewed component-host fixture, and mounts controls both standalone and
 *     inside the fixture host.
 *  2. Each test serves the built JS/CSS/HTML through Playwright route
 *     interception, so the components run as real compiled Svelte 5 code in
 *     the real browser — native keyboard behavior included.
 *
 * Scenario coverage (brief, feature-gate coverage owned by 01-verify):
 *  - controls standalone AND within a container (component-host)
 *  - keyboard activation (Enter/Space) via native button semantics
 *  - labels (label[for]↔input[id], getByLabel) and unique generated ids
 *  - error state (aria-invalid, aria-describedby → visible role="alert")
 *  - password field non-disclosure (masked input; value never in DOM)
 *  - instance independence (no shared module state between sibling controls)
 *  - token-backed colors (computed styles resolve from ui/tokens.css vars)
 */

const FIXTURE_URL = '/mai-ui-controls-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const VIRTUAL_ID = 'virtual:mai-ui-controls-entry';

const BUTTON = `${FRONTEND_ROOT}src/lib/ui/Button.svelte`;
const FIELD = `${FRONTEND_ROOT}src/lib/ui/Field.svelte`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

/** Fixture secret — synthetic test value, never a real credential. */
const SECRET = 'mai-fxt-pw-hunter2';

type Clicks = { standalone: number; hosted: number; loading: number };

/**
 * Minimal bundle shape the harness consumes. vite 7 keeps `RollupOutput`
 * internal (imports it from `rollup`, re-exports neither name), so this
 * boundary is typed structurally here and guarded after the build below.
 */
type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

declare global {
	interface Window {
		/** Activation counters written by the fixture entry (set before tests read it). */
		__maiClicks?: Clicks;
	}
}

let harnessJs = '';
let harnessCss = '';

/**
 * Virtual entry mounted in the browser. Plain JS (no runes file needed): the
 * controls are mounted programmatically, once standalone and once inside the
 * reviewed component-host fixture. Activation counters live on `window` so
 * tests can read native click outcomes without touching component internals.
 */
function entrySource(): string {
	return `
import { mount, createRawSnippet } from 'svelte';
import Button from ${JSON.stringify(BUTTON)};
import Field from ${JSON.stringify(FIELD)};
import ComponentHost from ${JSON.stringify(HOST)};
import ${JSON.stringify(TOKENS)};

const clicks = { standalone: 0, hosted: 0, loading: 0 };
window.__maiClicks = clicks;

// Raw snippets must return the HTML of a single element (svelte runtime
// contract), so label text is wrapped in a plain <span>.
const children = (text) => createRawSnippet(() => ({ render: () => \`<span>\${text}</span>\` }));
const count = (key) => () => { clicks[key] += 1; };

const app = document.getElementById('app');

// --- standalone controls (outside any container) ---
const standalone = document.createElement('section');
standalone.setAttribute('aria-label', 'Standalone controls');
standalone.setAttribute('data-testid', 'standalone');
app.appendChild(standalone);

mount(Field, {
	target: standalone,
	props: {
		label: 'Server label',
		hint: 'Display name only, never an identity',
		value: '',
		'data-testid': 'standalone-input'
	}
});
mount(Button, {
	target: standalone,
	props: {
		variant: 'secondary',
		onclick: count('standalone'),
		'data-testid': 'standalone-button',
		children: children('Enable server')
	}
});

// --- controls within the reviewed component-host container ---
mount(ComponentHost, {
	target: app,
	props: {
		label: 'MAI controls container',
		children: children('<div data-testid="hosted"></div><div data-testid="states"></div>')
	}
});

const hosted = document.querySelector('[data-testid="hosted"]');
mount(Field, {
	target: hosted,
	props: {
		label: 'API password',
		type: 'password',
		value: '',
		autocomplete: 'new-password',
		error: 'Password is required',
		'data-testid': 'password-input'
	}
});
mount(Field, {
	target: hosted,
	props: {
		label: 'Confirm password',
		type: 'password',
		value: '',
		'data-testid': 'confirm-input'
	}
});
mount(Button, {
	target: hosted,
	props: { onclick: count('hosted'), 'data-testid': 'hosted-button', children: children('Save profile') }
});

const states = document.querySelector('[data-testid="states"]');
mount(Button, {
	target: states,
	props: { loading: true, onclick: count('loading'), 'data-testid': 'loading-button', children: children('Deploy') }
});
mount(Button, {
	target: states,
	props: { variant: 'danger', 'data-testid': 'danger-button', children: children('Remove server') }
});
`;
}

/** Serves the virtual entry from the in-memory bundle; nothing hits disk. */
function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-ui-controls-harness',
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
			outDir: 'node_modules/.mai-ui-controls-fixture',
			rollupOptions: {
				input: VIRTUAL_ID,
				output: { entryFileNames: 'demo.js', assetFileNames: 'demo[extname]' }
			}
		}
	});

	// `build()` is one-shot (no watcher configured); structural boundary type above.
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
<title>MAI ui-controls fixture</title>
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

async function clicks(page: Page): Promise<Clicks> {
	const value = await page.evaluate(() => window.__maiClicks);
	if (!value) throw new Error('fixture activation counters are missing');
	return value;
}

test('controls mount standalone and inside the reviewed component-host fixture', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);

	// The reviewed fixture only flips data-mounted after a real client mount —
	// this proves the controls ran as mounted components, not static HTML.
	const host = page.getByTestId('component-host');
	await expect(host).toHaveAttribute('data-mounted', 'true');
	await expect(host).toBeVisible();

	// Labels resolve for screen readers / users on both instances.
	await expect(page.getByLabel('Server label')).toBeVisible();
	await expect(page.getByLabel('API password')).toBeVisible();
	await expect(page.getByLabel('Confirm password')).toBeVisible();

	// label[for] ↔ input[id] wiring with unique generated ids per instance.
	const wiring = await page.evaluate(() =>
		[...document.querySelectorAll('label.mai-field__label')].map((label) => {
			const forId = label.getAttribute('for');
			const input = forId ? document.getElementById(forId) : null;
			return {
				text: label.textContent ?? '',
				forId,
				inputId: input?.getAttribute('id') ?? null,
				inputTag: input?.tagName.toLowerCase() ?? null
			};
		})
	);
	expect(wiring.map((w) => w.text)).toEqual(['Server label', 'API password', 'Confirm password']);
	const forIds = wiring.map((w) => w.forId);
	expect(
		forIds.every((id, index) => forIds.indexOf(id, index + 1) === -1),
		'generated label targets must be unique per instance'
	).toBe(true);
	for (const w of wiring) {
		expect(w.forId, `label "${w.text}" must point at its input`).toBeTruthy();
		expect(w.forId).toBe(w.inputId);
		expect(w.inputTag).toBe('input');
	}

	expect(errors).toEqual([]);
});

test('buttons expose typed variants backed by design tokens and default to type="button"', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	expect(await page.getByTestId('standalone-button').getAttribute('type')).toBe('button');
	expect(await page.getByTestId('standalone-button').getAttribute('class')).toContain(
		'mai-button--secondary'
	);

	// Computed colors must resolve from the token layer, not hardcoded values.
	const primaryBg = await page
		.getByTestId('hosted-button')
		.evaluate((el) => getComputedStyle(el).backgroundColor);
	expect(primaryBg).toBe('rgb(185, 28, 28)'); // --mai-color-primary #b91c1c

	const dangerBg = await page
		.getByTestId('danger-button')
		.evaluate((el) => getComputedStyle(el).backgroundColor);
	expect(dangerBg).toBe('rgb(185, 28, 28)'); // --mai-status-danger-solid #b91c1c

	const secondaryBorder = await page
		.getByTestId('standalone-button')
		.evaluate((el) => getComputedStyle(el).borderTopColor);
	expect(secondaryBorder).toBe('rgb(42, 50, 65)'); // --mai-color-border #2a3241
});

test('buttons activate from the keyboard (Enter and Space) like native buttons', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	// Keyboard-only path: first Tab reaches the standalone input, second the
	// standalone button — DOM-order focus, no custom key handling needed.
	await page.keyboard.press('Tab');
	expect(
		await page.evaluate(() => document.activeElement?.getAttribute('data-testid'))
	).toBe('standalone-input');
	await page.keyboard.press('Tab');
	expect(
		await page.evaluate(() => document.activeElement?.getAttribute('data-testid'))
	).toBe('standalone-button');

	await page.keyboard.press('Enter');
	expect(await clicks(page)).toMatchObject({ standalone: 1, hosted: 0 });

	await page.keyboard.press(' '); // Space — the other native activation key
	expect(await clicks(page)).toMatchObject({ standalone: 2, hosted: 0 });
});

test('loading state disables the button and blocks activation', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	const loading = page.getByTestId('loading-button');
	await expect(loading).toBeDisabled();
	expect(await loading.getAttribute('aria-busy')).toBe('true');

	// Disabled buttons are not focusable in tab order (skipped by Tab in the
	// keyboard test) and never fire click — even a forced pointer event.
	await loading.click({ force: true });
	expect(await clicks(page)).toMatchObject({ loading: 0 });

	// The pending indicator is decorative and hidden from assistive tech.
	const spinner = loading.locator('span.mai-button__spinner');
	await expect(spinner).toHaveCount(1);
	expect(await spinner.getAttribute('aria-hidden')).toBe('true');
});

test('field error state is linked, announced and token-colored; clean fields stay clean', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const password = page.getByLabel('API password');
	expect(await password.getAttribute('aria-invalid')).toBe('true');

	const describedBy = await password.getAttribute('aria-describedby');
	expect(describedBy).toBeTruthy();
	const errorText = page.locator(`#${describedBy}`);
	await expect(errorText).toHaveText('Password is required');
	expect(await errorText.getAttribute('role')).toBe('alert');

	// The error is not color-only, but the color is also the danger token.
	const border = await password.evaluate((el) => getComputedStyle(el).borderTopColor);
	expect(border).toBe('rgb(252, 165, 165)'); // --mai-status-danger-text #fca5a5

	// Negative direction: a clean field has no invalid state…
	expect(await page.getByLabel('Confirm password').getAttribute('aria-invalid')).toBeNull();
	// …and the hint (if present) is linked through the same mechanism.
	const hintId = await page.getByLabel('Server label').getAttribute('aria-describedby');
	expect(hintId).toBeTruthy();
	await expect(page.locator(`#${hintId}`)).toHaveText('Display name only, never an identity');
});

test('password field masks its value and never discloses it in the DOM', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	const password = page.getByLabel('API password');
	await password.fill(SECRET);

	expect(await password.getAttribute('type')).toBe('password');
	expect(await password.inputValue()).toBe(SECRET); // binding holds the value
	// Disclosure proof: the typed value appears nowhere in the serialized DOM —
	// no attribute copy, no text node, no hidden echo of the field value.
	const html = await page.content();
	expect(html).not.toContain(SECRET);
	const bodyText = await page.evaluate(() => document.body.innerText);
	expect(bodyText).not.toContain(SECRET);
});

test('sibling field instances keep independent state (no shared module state)', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	await page.getByLabel('Server label').fill('alpha');
	await page.getByLabel('Confirm password').fill('beta');

	// A module-level shared value store would cross-fill these.
	expect(await page.getByLabel('Server label').inputValue()).toBe('alpha');
	expect(await page.getByLabel('Confirm password').inputValue()).toBe('beta');
	expect(await page.getByLabel('API password').inputValue()).toBe('');
});
