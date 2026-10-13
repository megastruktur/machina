import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * 01-window-types — real-browser proofs for the persisted window-descriptor
 * contract (windowed-workspace ADR §3, §8, §10.2 NT4, §11; contracts.md C01,
 * C08 rule 5, C11).
 *
 * Exercise, against the reviewed 01-web component-host fixture:
 *  - the frozen good/bad descriptor fixtures of ADR §11 validate/reject as
 *    specified (unknown kind, missing server_id, NaN/non-integer geometry,
 *    unknown schema major, forbidden title content, duplicate window_id);
 *  - hostile stored layouts drop entries with reasons or discard whole
 *    documents — never throw into the workspace, never import from data;
 *  - identity separation: window_id is an opaque client UUID, distinct from
 *    server/run ids; labels/IPs are never identity;
 *  - forward compatibility: unknown fields within schema v1 are ignored and
 *    never propagated (whitelist emission);
 *  - serialization: whitelist-only output, round-trip equality, forbidden
 *    content and invalid state refused;
 *  - export surface: pure module — only functions, frozen constants and the
 *    error class; no mutable module state (no SSR singleton, no hidden global
 *    "current server" mutation target).
 *
 * The harness is the same self-contained pattern as ui-controls.spec.ts: an
 * in-memory Vite production build (`write: false`, virtual entry) bundling the
 * real module + the reviewed fixture; Playwright route interception serves it;
 * no temp files, no backend, no committed fixture changes.
 */

const FIXTURE_URL = '/mai-window-types-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const VIRTUAL_ID = 'virtual:mai-window-types-entry';

const TYPES = `${FRONTEND_ROOT}src/lib/workspace/types.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;

/**
 * Registry kinds the probes validate against — stand-ins for the frozen
 * PanelRegistry set (owned by 01-panel-registry); "log-viewer" is the ADR §11
 * unregistered-kind fixture and must stay absent.
 */
const REGISTRY_KINDS = ['chat', 'model-settings', 'server-profile'];

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const WINDOW_ID_A = '0f0c2b1e-1c4a-4e5f-8a3b-2d6e9f0a1b2c';
const WINDOW_ID_B = '5b2e8d40-9a1b-4c3e-8f2d-7e6a5b4c3d21';
const SERVER_ID = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const RUN_ID = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';

/** ADR §8 good fixture — server-scoped chat bound to a run. */
const GOOD_SERVER_CHAT = {
	schema_version: 1,
	window_id: WINDOW_ID_A,
	panel_kind: 'chat',
	scope: { kind: 'server', server_id: SERVER_ID, object: { kind: 'run', id: RUN_ID } },
	geometry: { x: 120, y: 80, width: 720, height: 480 },
	presentation: 'normal',
	z: 3,
	title_override: null
};

/** ADR §11 good fixture — global settings view. */
const GOOD_GLOBAL_SETTINGS = {
	schema_version: 1,
	window_id: WINDOW_ID_B,
	panel_kind: 'model-settings',
	scope: { kind: 'global' },
	geometry: { x: 40, y: 40, width: 640, height: 400 },
	presentation: 'normal',
	z: 1,
	title_override: null
};

/** Normalized schema-v1 descriptor (envelope dropped, whitelist fields only). */
function normalized(raw: Record<string, unknown>): Record<string, unknown> {
	const { schema_version: _envelope, ...descriptor } = raw;
	return descriptor;
}

/**
 * Minimal bundle shape the harness consumes (same structural boundary as
 * ui-controls.spec.ts — vite 7 keeps Rollup's output types internal).
 */
type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

/** Probe results mirror the module's discriminated results across the CDP boundary. */
interface ProbeIssue {
	code: string;
	message: string;
}
type ProbeValidation =
	| { ok: true; value: Record<string, unknown> }
	| { ok: false; issue: ProbeIssue };
type ProbeDropped = { index: number; code: string; message: string };
type ProbeParse =
	| { status: 'discarded'; code: string; message: string }
	| { status: 'ok'; revision: number; windows: Record<string, unknown>[]; dropped: ProbeDropped[] };
type ProbeSerialize =
	| { ok: true; json: string }
	| { ok: false; name: string; code: string | null; message: string };

/** Shape the virtual entry exposes on `window` for the probes below. */
interface ProbeApi {
	validate(raw: unknown, kinds: string[]): ProbeValidation;
	parse(text: string, kinds: string[]): ProbeParse;
	serialize(windows: unknown[], revision: unknown, kinds: string[]): ProbeSerialize;
	surface(): string[];
	constants(): Record<string, unknown>;
	newWindowId(): string;
}

declare global {
	interface Window {
		__maiApi?: ProbeApi;
	}
}

let harnessJs = '';
let harnessCss = '';

/**
 * Virtual entry mounted in the browser. Exposes thin wrappers over the real
 * module (Sets are rebuilt in-browser — they cannot cross the CDP boundary)
 * and mounts the reviewed component-host fixture as the real-mount proof.
 */
function entrySource(): string {
	return `
import { mount } from 'svelte';
import ComponentHost from ${JSON.stringify(HOST)};
import * as ws from ${JSON.stringify(TYPES)};

window.__maiApi = {
	validate: (raw, kinds) => ws.validateWindowDescriptor(raw, new Set(kinds)),
	parse: (text, kinds) => ws.parseStoredLayout(text, new Set(kinds)),
	serialize: (windows, revision, kinds) => {
		try {
			return { ok: true, json: ws.serializeLayout(windows, revision, new Set(kinds)) };
		} catch (error) {
			return { ok: false, name: error.name, code: error.issue ? error.issue.code : null, message: error.message };
		}
	},
	surface: () => Object.keys(ws).sort(),
	constants: () => ({
		schemaVersion: ws.SCHEMA_VERSION,
		titleBarMin: ws.TITLE_BAR_MIN,
		defaultMinGeometry: ws.DEFAULT_MIN_GEOMETRY,
		maxTitleOverrideLength: ws.MAX_TITLE_OVERRIDE_LENGTH,
		scopeObjectKinds: [...ws.SCOPE_OBJECT_KINDS],
		presentations: [...ws.PRESENTATIONS],
		frozen: {
			titleBarMin: Object.isFrozen(ws.TITLE_BAR_MIN),
			defaultMinGeometry: Object.isFrozen(ws.DEFAULT_MIN_GEOMETRY),
			scopeObjectKinds: Object.isFrozen(ws.SCOPE_OBJECT_KINDS),
			presentations: Object.isFrozen(ws.PRESENTATIONS)
		}
	}),
	newWindowId: () => ws.createWindowId()
};

mount(ComponentHost, { target: document.getElementById('app'), props: { label: 'MAI window types probe' } });
`;
}

/** Serves the virtual entry from the in-memory bundle; nothing hits disk. */
function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-window-types-harness',
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
			outDir: 'node_modules/.mai-window-types-fixture',
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
	// Unlike ui-controls, this harness legitimately produces no CSS asset: the
	// contract module is logic-only and the fixture host carries no styles.
	if (!harnessJs) {
		throw new Error('harness bundle incomplete: missing JS chunk');
	}
});

test.beforeEach(async ({ page }) => {
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI window-types fixture</title>
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

/** Navigates and waits until the entry module has exposed the probe API. */
async function gotoProbe(page: Page): Promise<void> {
	await page.goto(FIXTURE_URL);

	// The entry is a deferred `<script type="module">`: under full-suite CPU
	// contention it can execute after `goto` resolves, so a bare `evaluate`
	// races it (observed: intermittent `__maiApi` undefined when 29 specs run
	// in parallel). waitForFunction retries — same contract as the fixture's
	// auto-retrying `toHaveAttribute('data-mounted', 'true')` below.
	await page.waitForFunction(() => window.__maiApi !== undefined, undefined, {
		timeout: 15_000
	});
}

/** Validates a descriptor in-browser against the stand-in registry kinds. */
async function validate(page: Page, raw: unknown): Promise<ProbeValidation> {
	return page.evaluate(
		([fixture, kinds]) => window.__maiApi!.validate(fixture, kinds),
		[raw, REGISTRY_KINDS] as const
	);
}

/** Parses a stored-layout document in-browser against the stand-in registry. */
async function parse(page: Page, text: string): Promise<ProbeParse> {
	return page.evaluate(
		([layout, kinds]) => window.__maiApi!.parse(layout, kinds),
		[text, REGISTRY_KINDS] as const
	);
}

/** Serializes in-browser; throws are returned as `{ ok: false, … }`. */
async function serialize(page: Page, windows: unknown[], revision: unknown): Promise<ProbeSerialize> {
	return page.evaluate(
		([list, rev, kinds]) => window.__maiApi!.serialize(list, rev, kinds),
		[windows, revision, REGISTRY_KINDS] as const
	);
}

test('probe surface mounts in the reviewed component-host and exports exactly the pure contract API', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	await gotoProbe(page);

	// Real client mount in the reviewed fixture (not static HTML).
	await expect(page.getByTestId('component-host')).toHaveAttribute('data-mounted', 'true');

	// Export surface: only functions, frozen constants and the error class —
	// no store, no mutable module state, no "current server" mutation target.
	const surface = await page.evaluate(() => window.__maiApi!.surface());
	expect(surface).toEqual([
		'DEFAULT_MIN_GEOMETRY',
		'LayoutSerializationError',
		'MAX_TITLE_OVERRIDE_LENGTH',
		'PRESENTATIONS',
		'SCHEMA_VERSION',
		'SCOPE_OBJECT_KINDS',
		'TITLE_BAR_MIN',
		'containsForbiddenContent',
		'createWindowId',
		'parseStoredLayout',
		'serializeLayout',
		'validateGeometry',
		'validateObjectRef',
		'validateScope',
		'validateTitleOverride',
		'validateWindowDescriptor'
	]);

	// Frozen contract constants (ADR §10.2 NT3/§8 values are contractual).
	const constants = await page.evaluate(() => window.__maiApi!.constants());
	expect(constants).toMatchObject({
		schemaVersion: 1,
		titleBarMin: { width: 64, height: 32 },
		defaultMinGeometry: { width: 320, height: 200 },
		maxTitleOverrideLength: 200,
		scopeObjectKinds: ['application', 'run', 'document', 'conversation'],
		presentations: ['normal', 'minimized', 'maximized'],
		frozen: {
			titleBarMin: true,
			defaultMinGeometry: true,
			scopeObjectKinds: true,
			presentations: true
		}
	});

	expect(errors).toEqual([]);
});

test('good descriptors from ADR §8/§11 validate to normalized schema v1', async ({ page }) => {
	await gotoProbe(page);

	// Exact deep equality: normalization carries exactly the schema fields.
	expect(await validate(page, GOOD_SERVER_CHAT)).toEqual({
		ok: true,
		value: normalized(GOOD_SERVER_CHAT)
	});
	expect(await validate(page, GOOD_GLOBAL_SETTINGS)).toEqual({
		ok: true,
		value: normalized(GOOD_GLOBAL_SETTINGS)
	});
});

test('window identity is an opaque client UUID, distinct from server/run ids', async ({
	page
}) => {
	await gotoProbe(page);

	// Two generated ids differ and are canonical UUIDs (§3.1).
	const idA = await page.evaluate(() => window.__maiApi!.newWindowId());
	const idB = await page.evaluate(() => window.__maiApi!.newWindowId());
	expect(idA).not.toEqual(idB);
	for (const id of [idA, idB]) {
		expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
	}

	// Non-identity shapes are rejected as window_id (C01 rule 1).
	for (const bad of ['chat-window-1', 42, null]) {
		const result = await validate(page, { ...GOOD_SERVER_CHAT, window_id: bad });
		expect(result).toMatchObject({ ok: false, issue: { code: 'bad_window_id' } });
	}

	// Pinned target: a label, an IP or a truncated id is never a server_id,
	// and a server scope without server_id is never "inferred" from a global
	// selection (C08 rule 5 / NT5 — the module has no selection to infer from).
	const withoutId = await validate(page, {
		...GOOD_SERVER_CHAT,
		scope: { kind: 'server', object: { kind: 'run', id: RUN_ID } }
	});
	expect(withoutId).toMatchObject({
		ok: false,
		issue: { code: 'scope_server_without_server_id' }
	});
	for (const label of ['web-01 (rack)', '10.0.0.7', SERVER_ID.slice(0, 8)]) {
		const result = await validate(page, {
			...GOOD_SERVER_CHAT,
			scope: { kind: 'server', server_id: label }
		});
		expect(result).toMatchObject({ ok: false, issue: { code: 'bad_server_id' } });
	}
});

test('scope is a typed union: server scope pins a validated object ref, global scope carries no target', async ({
	page
}) => {
	await gotoProbe(page);

	// Every contract object kind validates and survives normalization.
	for (const kind of ['application', 'run', 'document', 'conversation']) {
		const result = await validate(page, {
			...GOOD_SERVER_CHAT,
			scope: { kind: 'server', server_id: SERVER_ID, object: { kind, id: RUN_ID } }
		});
		expect(result).toMatchObject({
			ok: true,
			value: { scope: { kind: 'server', server_id: SERVER_ID, object: { kind, id: RUN_ID } } }
		});
	}

	// Outside the union → rejected.
	for (const kind of ['job', 'window', 'server', '']) {
		const result = await validate(page, {
			...GOOD_SERVER_CHAT,
			scope: { kind: 'server', server_id: SERVER_ID, object: { kind, id: RUN_ID } }
		});
		expect(result).toMatchObject({ ok: false, issue: { code: 'bad_object_ref' } });
	}

	// Non-UUID object id → rejected.
	const badRefId = await validate(page, {
		...GOOD_SERVER_CHAT,
		scope: { kind: 'server', server_id: SERVER_ID, object: { kind: 'run', id: 'run-7' } }
	});
	expect(badRefId).toMatchObject({ ok: false, issue: { code: 'bad_object_ref' } });

	// Unknown scope kind → rejected.
	const badScope = await validate(page, { ...GOOD_SERVER_CHAT, scope: { kind: 'launcher' } });
	expect(badScope).toMatchObject({ ok: false, issue: { code: 'bad_scope' } });

	// A global scope normalizes to exactly { kind: "global" } — stray fields on
	// hostile input never survive into state.
	const global = await validate(page, {
		...GOOD_GLOBAL_SETTINGS,
		scope: { kind: 'global', server_id: SERVER_ID, leaked: 'payload' }
	});
	expect(global).toEqual({ ok: true, value: normalized(GOOD_GLOBAL_SETTINGS) });
});

test('geometry accepts integers only; NaN, floats, strings and non-positive sizes are rejected', async ({
	page
}) => {
	await gotoProbe(page);

	// Offscreen stored coordinates stay valid (restore clamps, NT3/PT6);
	// non-positive sizes never do.
	const offscreen = await validate(page, {
		...GOOD_SERVER_CHAT,
		geometry: { x: -4000, y: -2000, width: 720, height: 480 }
	});
	expect(offscreen).toEqual({
		ok: true,
		value: {
			...normalized(GOOD_SERVER_CHAT),
			geometry: { x: -4000, y: -2000, width: 720, height: 480 }
		}
	});

	// Built in-browser: NaN/Infinity cannot cross the CDP boundary as numbers.
	for (const badWidth of ['NaN', 'Infinity'] as const) {
		const result = await page.evaluate(([fixture, kinds, kind]) => {
			const raw = {
				...fixture,
				geometry: { x: 120, y: 80, width: kind === 'NaN' ? NaN : Infinity, height: 480 }
			};
			return window.__maiApi!.validate(raw, kinds);
		}, [GOOD_SERVER_CHAT, REGISTRY_KINDS, badWidth] as const);
		expect(result).toMatchObject({ ok: false, issue: { code: 'bad_geometry' } });
	}

	// Floats, numeric strings and null (the NaN JSON round-trip artifact).
	for (const width of [720.5, '720', null]) {
		const result = await validate(page, {
			...GOOD_SERVER_CHAT,
			geometry: { x: 120, y: 80, width, height: 480 }
		});
		expect(result).toMatchObject({ ok: false, issue: { code: 'bad_geometry' } });
	}

	for (const geometry of [null, { x: 1, y: 2, width: 100 }, '720x480']) {
		const result = await validate(page, { ...GOOD_SERVER_CHAT, geometry });
		expect(result).toMatchObject({ ok: false, issue: { code: 'bad_geometry' } });
	}

	const zeroHeight = await validate(page, {
		...GOOD_SERVER_CHAT,
		geometry: { x: 0, y: 0, width: 720, height: 0 }
	});
	expect(zeroHeight).toMatchObject({ ok: false, issue: { code: 'bad_geometry' } });
});

test('ADR §11 bad-fixture set: unknown panel kind and bad presentation/z drop the entry', async ({
	page
}) => {
	await gotoProbe(page);

	// "panel_kind": "log-viewer" not in registry → unavailable panel.
	const unknownKind = await validate(page, { ...GOOD_SERVER_CHAT, panel_kind: 'log-viewer' });
	expect(unknownKind).toMatchObject({
		ok: false,
		issue: {
			code: 'unknown_panel_kind',
			message: 'panel kind "log-viewer" is not registered; this window is unavailable'
		}
	});

	// Slug-shape violations cannot be registry kinds at all.
	for (const kind of ['', 'Chat', 'log_viewer', 'chat window']) {
		const result = await validate(page, { ...GOOD_SERVER_CHAT, panel_kind: kind });
		expect(result).toMatchObject({ ok: false, issue: { code: 'bad_panel_kind' } });
	}

	for (const presentation of ['hidden', 'MINIMIZED', null]) {
		const result = await validate(page, { ...GOOD_SERVER_CHAT, presentation });
		expect(result).toMatchObject({ ok: false, issue: { code: 'bad_presentation' } });
	}

	for (const z of [-1, 1.5, '3', null]) {
		const result = await validate(page, { ...GOOD_SERVER_CHAT, z });
		expect(result).toMatchObject({ ok: false, issue: { code: 'bad_z' } });
	}

	const notAnObject = await validate(page, 'chat');
	expect(notAnObject).toMatchObject({ ok: false, issue: { code: 'not_an_object' } });
});

test('forward compatibility: unknown fields within schema v1 are ignored and never propagated', async ({
	page
}) => {
	await gotoProbe(page);

	// A descriptor carrying unknown future fields (and an import-looking
	// payload) validates; exact deep equality below proves the normalized
	// result contains only schema fields — unknown data is neither executed
	// nor persisted (no dynamic import from data, ADR §7.1/NT4).
	const forwardCompatible = await validate(page, {
		...GOOD_SERVER_CHAT,
		unknown_future_field: { nested: [1, 2, 3] },
		import_path: '../../evil',
		floating_point_extra: 1.5
	});
	expect(forwardCompatible).toEqual({ ok: true, value: normalized(GOOD_SERVER_CHAT) });
});

test('hostile stored layouts: bad entries drop with reasons, survivors stay, nothing throws', async ({
	page
}) => {
	await gotoProbe(page);

	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));

	const hostile = {
		schema_version: 1,
		revision: 7,
		windows: [
			GOOD_SERVER_CHAT, // survives
			{ ...GOOD_GLOBAL_SETTINGS, panel_kind: 'log-viewer' }, // §11: unknown kind
			{ ...GOOD_SERVER_CHAT, window_id: WINDOW_ID_B, scope: { kind: 'server' } }, // §11: no server_id
			{
				...GOOD_SERVER_CHAT,
				window_id: WINDOW_ID_B,
				geometry: { x: null, y: 80, width: 720, height: 480 } // NaN round-trips to null
			},
			{
				...GOOD_SERVER_CHAT,
				window_id: WINDOW_ID_B,
				geometry: { x: 1.5, y: 80, width: 720, height: 480 }
			},
			{ ...GOOD_SERVER_CHAT, z: 9 }, // duplicate of the accepted window_id → keep first, drop rest
			'not-a-descriptor'
		]
	};

	const result = await parse(page, JSON.stringify(hostile));
	expect(result).toEqual({
		status: 'ok',
		revision: 7,
		windows: [normalized(GOOD_SERVER_CHAT)],
		dropped: [
			{
				index: 1,
				code: 'unknown_panel_kind',
				message: 'panel kind "log-viewer" is not registered; this window is unavailable'
			},
			{
				index: 2,
				code: 'scope_server_without_server_id',
				message:
					'server-scoped windows require an explicit server_id; the target is never inferred from a global selection'
			},
			{ index: 3, code: 'bad_geometry', message: 'geometry x must be an integer' },
			{ index: 4, code: 'bad_geometry', message: 'geometry x must be an integer' },
			{
				index: 5,
				code: 'duplicate_window_id',
				message: 'duplicate window_id; keeping the first occurrence'
			},
			{ index: 6, code: 'not_an_object', message: 'window descriptor must be an object' }
		]
	});

	expect(errors).toEqual([]);
});

test('hostile envelope: unparsable JSON, unknown schema major, bad revision or shape discards the whole document', async ({
	page
}) => {
	await gotoProbe(page);

	const cases: Array<{ name: string; text: string; code: string }> = [
		{ name: 'unparsable JSON', text: '{not json', code: 'unparsable_json' },
		{
			name: 'unknown major version',
			text: JSON.stringify({ schema_version: 2, revision: 1, windows: [GOOD_SERVER_CHAT] }),
			code: 'unknown_schema_version'
		},
		{
			name: 'string major version',
			text: JSON.stringify({ schema_version: '1', revision: 1, windows: [] }),
			code: 'unknown_schema_version'
		},
		{
			name: 'negative revision',
			text: JSON.stringify({ schema_version: 1, revision: -1, windows: [] }),
			code: 'bad_revision'
		},
		{
			name: 'float revision',
			text: JSON.stringify({ schema_version: 1, revision: 1.5, windows: [] }),
			code: 'bad_revision'
		},
		{
			name: 'string revision',
			text: JSON.stringify({ schema_version: 1, revision: '7', windows: [] }),
			code: 'bad_revision'
		},
		{
			name: 'missing windows array',
			text: JSON.stringify({ schema_version: 1, revision: 1 }),
			code: 'bad_layout_shape'
		},
		{
			name: 'array document',
			text: JSON.stringify([GOOD_SERVER_CHAT]),
			code: 'bad_layout_shape'
		}
	];

	for (const { text, code } of cases) {
		const result = await parse(page, text);
		expect(result).toMatchObject({ status: 'discarded', code });
		expect(result).toHaveProperty('message');
		expect(result).not.toHaveProperty('windows');
	}

	// Unknown top-level fields within schema v1 are ignored (§8 rule 2).
	const extraTopLevel = await parse(
		page,
		JSON.stringify({
			schema_version: 1,
			revision: 1,
			windows: [GOOD_SERVER_CHAT],
			unknown_future_setting: true
		})
	);
	expect(extraTopLevel).toMatchObject({ status: 'ok', revision: 1 });
});

test('duplicate window_id in one layout deduplicates deterministically (keep first)', async ({
	page
}) => {
	await gotoProbe(page);

	const layout = {
		schema_version: 1,
		revision: 0,
		windows: [
			{ ...GOOD_SERVER_CHAT, z: 1 },
			{ ...GOOD_SERVER_CHAT, z: 9, title_override: 'Second copy' }
		]
	};
	const result = await parse(page, JSON.stringify(layout));
	expect(result).toEqual({
		status: 'ok',
		revision: 0,
		windows: [{ ...normalized(GOOD_SERVER_CHAT), z: 1 }],
		dropped: [
			{
				index: 1,
				code: 'duplicate_window_id',
				message: 'duplicate window_id; keeping the first occurrence'
			}
		]
	});
});

test('forbidden content classes never validate and never serialize (ADR §11 severity-1 row)', async ({
	page
}) => {
	await gotoProbe(page);

	for (const title of [
		'token=sk-fxt-abc12345',
		'password: hunter2',
		'Authorization: Bearer abcdef123456',
		'-----BEGIN RSA PRIVATE KEY-----',
		'ghp_fxtabc1234567890'
	]) {
		const result = await validate(page, { ...GOOD_SERVER_CHAT, title_override: title });
		expect(result).toMatchObject({ ok: false, issue: { code: 'forbidden_content' } });
	}

	// Long free text is domain payload, not a display label.
	const longPayload = await validate(page, {
		...GOOD_SERVER_CHAT,
		title_override: 'x'.repeat(201)
	});
	expect(longPayload).toMatchObject({ ok: false, issue: { code: 'bad_title_override' } });

	// A legitimate label at the cap still validates.
	const atCap = await validate(page, {
		...GOOD_SERVER_CHAT,
		title_override: 'Deploy assistant'.padEnd(200, '.')
	});
	expect(atCap).toMatchObject({ ok: true, value: { title_override: expect.any(String) } });

	// Serialization layer refuses forbidden content with the dedicated error…
	const withSecret = await serialize(
		page,
		[{ ...GOOD_SERVER_CHAT, title_override: 'pwd=hunter2' }],
		4
	);
	expect(withSecret).toMatchObject({
		ok: false,
		name: 'LayoutSerializationError',
		code: 'forbidden_content'
	});

	// …and refuses invalid state (duplicate ids, bad revision) — state must
	// already be validated; serialization never launders it into storage.
	const duplicated = await serialize(
		page,
		[GOOD_SERVER_CHAT, { ...GOOD_GLOBAL_SETTINGS, window_id: WINDOW_ID_A }],
		4
	);
	expect(duplicated).toMatchObject({ ok: false, name: 'LayoutSerializationError' });
	for (const revision of [-1, 1.5, '7']) {
		const badRevision = await serialize(page, [GOOD_SERVER_CHAT], revision);
		expect(badRevision).toMatchObject({
			ok: false,
			name: 'LayoutSerializationError',
			code: 'bad_revision'
		});
	}
});

test('serialize → parse round-trips exactly, emitting only schema fields', async ({ page }) => {
	await gotoProbe(page);

	const dirtyInput = { ...GOOD_SERVER_CHAT, must_not_persist: 'transient runtime extra' };
	const serialized = await serialize(page, [dirtyInput, GOOD_GLOBAL_SETTINGS], 12);
	if (!('json' in serialized)) {
		throw new Error(`serialization failed: ${serialized.message}`);
	}

	// Whitelist emission: the stored document carries only schema fields, so
	// runtime extras can never leak into persistence (ADR §8).
	const doc = JSON.parse(serialized.json) as Record<string, unknown>;
	expect(Object.keys(doc).sort()).toEqual(['revision', 'schema_version', 'windows']);
	expect(doc).toMatchObject({ schema_version: 1, revision: 12 });

	const reparsed = await parse(page, serialized.json);
	expect(reparsed).toEqual({
		status: 'ok',
		revision: 12,
		windows: [normalized(GOOD_SERVER_CHAT), normalized(GOOD_GLOBAL_SETTINGS)],
		dropped: []
	});
});
