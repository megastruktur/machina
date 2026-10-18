import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 10-web-evidence — real-browser proofs for the reusable observation/finding
 * evidence details component (`ObservationDetails.svelte`).
 *
 * Harness convention (reviewed): an in-memory vite build mounts the REAL
 * component (relative imports, no SvelteKit aliases) into several labeled
 * sections of one page served entirely from route interception — no disk
 * artifacts, no backend, no invented endpoints. The reviewed `component-host`
 * fixture proves real client mounts via its `data-mounted` flip; activation
 * counters on `window` prove the attach/re-evaluate handoffs without touching
 * component internals.
 *
 * Covered acceptance (brief 10-web-evidence):
 * 1. bounded details (processes/network/timers/security rows) expose
 *    timestamps/source/provenance and attach the permitted context ref;
 * 2. unknown / permission-gap / empty are distinct truthful states; hostile
 *    evidence HTML is inert;
 * 3. reusable presentational component only: explicit scope, no global
 *    server target, instances independent (no shared module state).
 */

const FIXTURE_URL = '/mai-observation-details-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const VIRTUAL_ID = 'virtual:mai-observation-details-entry';

const DETAILS = `${FRONTEND_ROOT}src/lib/components/ObservationDetails.svelte`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const OTHER_SERVER = '9d2e3f4a-5b6c-4d7e-8f90-1b2c3d4e5f6a';
const OBS_ID = 'aa111111-1111-4222-8333-444455556666';
const FINDING_ID = 'bb222222-2222-4333-8444-555566667777';
const APPLICATION_ID = 'cc333333-3333-4444-8555-666677778888';

type AttachCall = { server_id: string; kind: string; context_ref: string };
type EvaluateCall = { server_id: string };
type Calls = { attach: AttachCall[]; evaluate: EvaluateCall[] };

declare global {
	interface Window {
		/** Activation records written by the fixture entry (set before tests read it). */
		__maiEvidence?: Calls;
	}
}

let harnessJs = '';
let harnessCss = '';

/**
 * Wire-view builders mirroring the generated schema exactly
 * (`components['schemas']['ObservationView' | 'FindingView' | 'EvaluateResult']`).
 * Field names are the wire names; extra/unknown keys would be a schema drift
 * and must fail here, not silently.
 */
function observationRow(overrides: Record<string, unknown> = {}): string {
	return JSON.stringify({
		observation_id: OBS_ID,
		server_id: SERVER,
		application_id: null,
		source: 'process',
		method: 'command',
		quality: 'measured',
		metric: 'proc.zombies',
		value: 'count=3',
		observed_at: '2026-10-17T09:15:04Z',
		collected_at: '2026-10-17T09:15:05Z',
		fresh_until: '2026-10-17T09:45:04Z',
		freshness: 'stale',
		seq: 42,
		context_ref: `observation:${OBS_ID}`,
		...overrides
	});
}

function findingRow(overrides: Record<string, unknown> = {}): string {
	return JSON.stringify({
		finding_id: FINDING_ID,
		server_id: SERVER,
		application_id: null,
		observation_id: OBS_ID,
		kind: 'process.zombies',
		dedup_key: 'count',
		severity: 'warning',
		summary: '3 zombie processes counted by the collector',
		detail:
			'Reap or restart the parent of the listed zombies. This monitor only reports; it never changes the target.',
		opened_at: '2026-10-17T09:15:06Z',
		updated_at: '2026-10-17T09:30:06Z',
		resolved_at: null,
		open: true,
		context_ref: `finding:${FINDING_ID}`,
		...overrides
	});
}

const EVAL_RESULT = JSON.stringify({
	server_id: SERVER,
	evaluated: 2,
	outcomes: [
		{
			alert_id: 'dd444444-4444-4555-8666-777788889999',
			dedup_key: 'count',
			disposition: 'notified',
			delivery: 'delivered',
			channels_attempted: ['web_inbox']
		},
		{
			alert_id: 'ee555555-5555-4666-8777-888899990000',
			dedup_key: 'top_rss',
			disposition: 'suppressed',
			delivery: 'acked',
			channels_attempted: []
		}
	]
});

/**
 * Virtual entry mounted in the browser: the component is mounted standalone
 * and inside the reviewed component-host fixture, plus dedicated hosts for
 * each state fixture. Callbacks record the EXACT payload objects they receive
 * so tests can assert the handoff carries nothing beyond identity + bounded
 * context reference.
 */
function entrySource(): string {
	return `
import { mount, createRawSnippet } from 'svelte';
import Details from ${JSON.stringify(DETAILS)};
import ComponentHost from ${JSON.stringify(HOST)};
import ${JSON.stringify(TOKENS)};

const calls = { attach: [], evaluate: [] };
window.__maiEvidence = calls;
const onattach = (payload) => calls.attach.push(payload);
const onevaluate = (payload) => calls.evaluate.push(payload);

const obs = ${observationRow()};
const finding = ${findingRow()};

const hosts = [
	['primary', { serverId: '${SERVER}', observation: obs, finding, onattach, onevaluate }],
	['resolved-finding', {
		serverId: '${SERVER}',
		observation: null,
		finding: ${findingRow({
			resolved_at: '2026-10-17T10:00:00Z',
			open: false,
			severity: 'info'
		})},
		onattach
	}],
	['empty', { serverId: '${SERVER}', observation: null, finding: null }],
	['unknown', {
		serverId: '${SERVER}',
		observation: ${observationRow({
			quality: 'unknown',
			metric: null,
			value: null,
			observed_at: null,
			fresh_until: null,
			freshness: 'unknown'
		})},
		finding: null
	}],
	['gap', {
		serverId: '${SERVER}',
		observation: ${observationRow({
			quality: 'degraded',
			metric: 'proc.unreadable',
			value: 'unreadable=7',
			freshness: 'fresh'
		})},
		finding: ${findingRow({
			kind: 'process.read_gaps',
			dedup_key: 'unreadable',
			summary: '7 /proc entries unreadable to the collector identity (permission gap; sample is partial)'
		})}
	}],
	['hostile', {
		serverId: '${SERVER}',
		observation: null,
		finding: ${findingRow({
			summary: '<img src=x onerror="window.__maiPwned=true">',
			detail:
				'<script>window.__maiPwned=true</script>Run <b onclick="window.__maiPwned=true">rm -rf /tmp/mai-fxt-10-web-evidence</b> and <iframe src=//evil.example></iframe>',
			kind: 'security.firewall_rules'
		})}
	}],
	['foreign', {
		serverId: '${SERVER}',
		observation: ${observationRow({ server_id: '${OTHER_SERVER}' })},
		finding: ${findingRow({ server_id: '${OTHER_SERVER}' })}
	}],
	['stale-eval', {
		serverId: '${SERVER}',
		observation: null,
		finding: null,
		evaluateResult: ${JSON.stringify({ server_id: OTHER_SERVER, evaluated: 9, outcomes: [] })},
		onevaluate
	}]
];

const app = document.getElementById('app');
// Raw snippets must return the HTML of a single element (svelte runtime
// contract) — the reviewed ui-controls convention for hosted children.
const hostedChildren = createRawSnippet(() => ({
	render: () => '<div data-testid="hosted-evidence"></div>'
}));
for (const [name, props] of hosts) {
	const section = document.createElement('section');
	section.setAttribute('aria-label', name);
	section.setAttribute('data-testid', 'host-' + name);
	app.appendChild(section);
	if (name === 'primary') {
		mount(ComponentHost, {
			target: section,
			props: { label: 'MAI evidence container', children: hostedChildren }
		});
		const hosted = section.querySelector('[data-testid="hosted-evidence"]');
		mount(Details, { target: hosted, props });
	} else {
		mount(Details, { target: section, props });
	}
}
`;
}

/** Serves the virtual entry from the in-memory bundle; nothing hits disk. */
function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-observation-details-harness',
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
			outDir: 'node_modules/.mai-observation-details-fixture',
			rollupOptions: {
				input: VIRTUAL_ID,
				output: { entryFileNames: 'demo.js', assetFileNames: 'demo[extname]' }
			}
		}
	});

	// `build()` is one-shot; structural boundary type as in the reviewed specs.
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

test.beforeEach(async ({ page }) => {
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI observation-details fixture</title>
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

async function calls(page: Page): Promise<Calls> {
	const value = await page.evaluate(() => window.__maiEvidence);
	// The guard keeps a broken harness from silently passing as "zero calls".
	if (!value) throw new Error('fixture activation records are missing');
	return value;
}

test('evidence details mount standalone and inside the reviewed component-host fixture', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);

	// Real client mount proof: the reviewed fixture flips data-mounted only
	// after Details actually mounted inside it (not static HTML).
	const host = page.getByTestId('component-host');
	await expect(host).toHaveAttribute('data-mounted', 'true');
	const hosted = host.getByTestId('evidence-details');
	await expect(hosted).toBeVisible();

	// Every dedicated state host rendered one details instance.
	for (const name of [
		'primary',
		'resolved-finding',
		'empty',
		'unknown',
		'gap',
		'hostile',
		'foreign',
		'stale-eval'
	]) {
		await expect(page.getByTestId(`host-${name}`).getByTestId('evidence-details')).toBeVisible();
	}

	expect(errors).toEqual([]);
});

test('process observation details expose source, quality, freshness and every timestamp', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const scope = page.getByTestId('host-primary');
	await expect(scope.getByTestId('observation-source')).toHaveText('source: process');
	await expect(scope.getByTestId('observation-method')).toHaveText('method: command');
	await expect(scope.getByTestId('observation-quality')).toHaveText('quality: measured');
	await expect(scope.getByTestId('observation-freshness')).toHaveText('freshness: stale');

	// Bounded measurement + full provenance with explicit UTC timestamps.
	await expect(scope.getByTestId('observation-metric')).toHaveText('proc.zombies');
	await expect(scope.getByTestId('observation-value')).toHaveText('count=3');
	await expect(scope.getByTestId('observation-id')).toHaveText(OBS_ID);
	await expect(scope.getByTestId('observation-observed-at')).toHaveText('2026-10-17T09:15:04Z');
	await expect(scope.getByTestId('observation-collected-at')).toHaveText('2026-10-17T09:15:05Z');
	await expect(scope.getByTestId('observation-fresh-until')).toHaveText('2026-10-17T09:45:04Z');
	await expect(scope.getByTestId('observation-seq')).toHaveText('42');

	// The finding sits in the same drawer with provenance inspection.
	await expect(scope.getByTestId('finding-kind')).toHaveText('process.zombies');
	await expect(scope.getByTestId('finding-severity')).toHaveText('warning');
	await expect(scope.getByTestId('finding-state')).toHaveText('open');
	await expect(scope.getByTestId('finding-id')).toHaveText(FINDING_ID);
	await expect(scope.getByTestId('finding-dedup-key')).toHaveText('count');
	await expect(scope.getByTestId('finding-observation-id')).toHaveText(OBS_ID);
	await expect(scope.getByTestId('finding-opened-at')).toHaveText('2026-10-17T09:15:06Z');
	await expect(scope.getByTestId('finding-updated-at')).toHaveText('2026-10-17T09:30:06Z');

	// Remediation guidance renders as guidance text.
	await expect(scope.getByTestId('finding-detail')).toContainText('This monitor only reports');
});

test('a resolved finding shows its resolution time instead of the open badge', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	const scope = page.getByTestId('host-resolved-finding');
	await expect(scope.getByTestId('finding-state')).toHaveText('resolved');
	await expect(scope.getByTestId('finding-severity')).toHaveText('info');
	await expect(scope.getByTestId('finding-resolved-at')).toHaveText('2026-10-17T10:00:00Z');
	await expect(scope.getByTestId('finding-opened-at')).toHaveText('2026-10-17T09:15:06Z');
});

test('attaching hands only the bounded permitted context ref to the chat seam', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const scope = page.getByTestId('host-primary');
	await scope.getByTestId('finding-attach').click();
	await scope.getByTestId('observation-attach').click();

	const recorded = await calls(page);
	expect(recorded.attach).toEqual([
		{ server_id: SERVER, kind: 'finding', context_ref: `finding:${FINDING_ID}` },
		{ server_id: SERVER, kind: 'observation', context_ref: `observation:${OBS_ID}` }
	]);
	// Deep-equal above already proves the payload carries no payload/detail
	// fields — only identity + the bounded context reference.
	expect(recorded.evaluate).toEqual([]);
});

test('finding re-evaluation emits the explicit server scope and renders only its own round', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const scope = page.getByTestId('host-primary');
	const button = scope.getByTestId('evaluate-button');
	await button.click();

	expect(await calls(page)).toEqual({
		attach: [],
		evaluate: [{ server_id: SERVER }]
	});

	// The stale-eval host carries a result from ANOTHER server: nothing renders.
	const stale = page.getByTestId('host-stale-eval');
	await expect(stale.getByTestId('evaluate-button')).toBeVisible();
	await expect(stale.getByTestId('evaluate-summary')).toHaveCount(0);
	await expect(stale.getByTestId('evaluate-outcomes')).toHaveCount(0);
});

test('unknown, permission-gap and empty are three visibly different truthful states', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const empty = page.getByTestId('host-empty').getByTestId('evidence-details');
	await expect(empty).toHaveAttribute('data-state', 'empty');
	await expect(empty.getByTestId('evidence-empty')).toContainText('No evidence is recorded');
	// Empty renders no rows at all.
	await expect(empty.getByTestId('evidence-finding')).toHaveCount(0);
	await expect(empty.getByTestId('evidence-observation')).toHaveCount(0);

	const unknown = page.getByTestId('host-unknown').getByTestId('evidence-details');
	await expect(unknown).toHaveAttribute('data-quality', 'unknown');
	const unknownNote = unknown.getByTestId('evidence-unknown');
	await expect(unknownNote).toContainText('No sample was taken');
	await expect(unknownNote).toContainText('not a zero value and not an empty history');
	// The unknown row still exposes its provenance, but no measurement exists.
	await expect(unknown.getByTestId('observation-source')).toHaveText('source: process');
	await expect(unknown.getByTestId('observation-no-measurement')).toBeVisible();
	await expect(unknown.getByTestId('observation-measurement')).toHaveCount(0);
	await expect(unknown.getByTestId('observation-observed-at')).toHaveCount(0);

	const gap = page.getByTestId('host-gap').getByTestId('evidence-details');
	await expect(gap).toHaveAttribute('data-permission-gap', 'true');
	await expect(gap.getByTestId('evidence-permission-gap')).toContainText(
		'Permission boundary on the target'
	);
	// The partial sample is still shown — honestly marked degraded.
	await expect(gap.getByTestId('observation-quality')).toHaveText('quality: degraded');
	await expect(gap.getByTestId('observation-value')).toHaveText('unreadable=7');
	await expect(gap.getByTestId('finding-kind')).toHaveText('process.read_gaps');
});

// --- negative controls (real safety boundaries) -----------------------------

test('negative control: hostile log/evidence HTML stays inert text', async ({ page }) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await page.goto(FIXTURE_URL);

	const scope = page.getByTestId('host-hostile');
	const details = scope.getByTestId('evidence-details');

	// The hostile strings are present as literal text…
	await expect(details.getByTestId('finding-summary')).toHaveText(
		'<img src=x onerror="window.__maiPwned=true">'
	);
	await expect(details.getByTestId('finding-detail')).toContainText(
		'<script>window.__maiPwned=true</script>'
	);
	await expect(details.getByTestId('finding-detail')).toContainText(
		'<b onclick="window.__maiPwned=true">rm -rf /tmp/mai-fxt-10-web-evidence</b>'
	);

	// …but nothing was parsed into elements and nothing executed.
	expect(await scope.locator('img, script, iframe, b').count()).toBe(0);
	expect(await page.evaluate(() => window.__maiPwned)).toBeUndefined();
	expect(errors).toEqual([]);
});

test('negative control: evidence of another server scope is refused, not rendered', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const scope = page.getByTestId('host-foreign');
	const details = scope.getByTestId('evidence-details');
	await expect(details).toHaveAttribute('data-state', 'scope-mismatch');
	await expect(details.getByTestId('evidence-scope-mismatch')).toBeVisible();

	// No row content, no provenance, no attach surface for foreign evidence.
	await expect(details.getByTestId('evidence-finding')).toHaveCount(0);
	await expect(details.getByTestId('evidence-observation')).toHaveCount(0);
	await expect(scope.getByText('3 zombie processes counted by the collector')).toHaveCount(0);
	await expect(scope.getByTestId('finding-attach')).toHaveCount(0);
	await expect(scope.getByTestId('observation-attach')).toHaveCount(0);

	// And no activation can leak the foreign scope through the seam.
	expect(await calls(page)).toEqual({ attach: [], evaluate: [] });
});

test('negative control: a cached re-evaluation round of a previous server never renders', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);

	const stale = page.getByTestId('host-stale-eval');
	// evaluateResult carries server_id of ANOTHER server with evaluated=9:
	// neither the summary nor any outcome row may appear.
	await expect(stale.getByTestId('evaluate-summary')).toHaveCount(0);
	await expect(stale.getByTestId('evaluate-outcomes')).toHaveCount(0);
	await expect(stale.getByText('Evaluated 9')).toHaveCount(0);
});

test('sibling instances keep independent state (no shared module state)', async ({ page }) => {
	await page.goto(FIXTURE_URL);

	// Two different hosts attach their OWN context refs; neither leaks into
	// the other instance and no instance accumulates the other's calls.
	const primary = page.getByTestId('host-primary');
	const resolved = page.getByTestId('host-resolved-finding');

	await resolved.getByTestId('finding-attach').click();
	await primary.getByTestId('observation-attach').click();

	expect(await calls(page)).toEqual({
		attach: [
			{ server_id: SERVER, kind: 'finding', context_ref: `finding:${FINDING_ID}` },
			{ server_id: SERVER, kind: 'observation', context_ref: `observation:${OBS_ID}` }
		],
		evaluate: []
	});
});
