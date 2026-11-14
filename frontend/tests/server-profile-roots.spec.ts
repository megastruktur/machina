import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * FRR T4 — real-browser proofs for the file-read-roots editor in the server
 * profile panel (frontend/src/lib/panels/ServerProfilePanel.svelte).
 *
 * Harness (the mock-API pattern of tests/server-profile.spec.ts, trimmed to
 * this slice): an IN-MEMORY Vite production build of a virtual app mounting
 * the REAL panel through the REAL PanelHost + feature entry, with a fixture
 * in-page API implementing the panel's typed service shape. The fixture API
 * records every call on `window` so tests assert what the panel actually
 * SENT (the wire payload), never component internals — and it mirrors the
 * real store including `file_read_roots` (FRR T3) with the same stale-caller
 * 409 `conflict` semantics (servers.py). No live backend.
 *
 * Scenario coverage (T4_SPEC):
 *  1. Load: roots from GET render as chips + read-view list.
 *  2. Add a valid root → chip appears; save payload carries it.
 *  3. Remove a root → save payload reflects the removal.
 *  4. Invalid adds (relative path, '/', duplicate) → inline error, no chip,
 *     no save mutation (form stays clean, nothing sent).
 *  5. Empty list → fail-closed hint visible; explicit [] is SENT on save.
 *  6. Conflict (409) on save → banner, unsaved roots kept, no auto-retry;
 *     explicit Reload recovers.
 */

const FIXTURE_URL = '/mai-server-profile-roots-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-server-profile-roots-app.svelte';
const ENTRY_ID = 'virtual:mai-server-profile-roots-entry.js';

const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/02-web-server.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixture (opaque UUID per contracts.md C01 rule 1). */
const SERVER_A = '9a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d';
const WINDOW_A = '7d8e9f0a-1b2c-4d3e-8f4a-5b6c7d8e9f0a';

const HOST_KEY = 'SHA256:F1xTur3F1nGeRPr1nT0000000000000000000000000';
/** The frozen stale-edit conflict message of the profile API (servers.py). */
const CONFLICT_MESSAGE = 'the profile changed since the last observed revision';

type EditFailure = { status: number; code: string; message: string };

interface WindowApi {
	calls: Array<{ op: string; serverId: string; payload?: unknown }>;
	db: Record<string, Record<string, unknown>>;
	failNextEdit(failure: EditFailure | null): void;
}

declare global {
	interface Window {
		__maiRootsApi?: WindowApi;
	}
}

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; fileName: string; isEntry: boolean; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

let harnessJs = '';
let harnessCss = '';

/**
 * The fixture app (runes mode, like the sibling panel harnesses). The fixture
 * backend is the only writer; it mirrors the REAL servers.py semantics: the
 * observed-revision pre-check ⇒ value-free 409 `conflict`, and the store
 * persists `file_read_roots` from the payload (FRR T3).
 */
const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { ApiError } from '${CLIENT}';
	import { serverProfileEntry, serverProfileServices } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';

	function makeProfile(serverId) {
		return {
			schema_version: 1,
			server_id: serverId,
			name: 'alpha',
			prompt_revision: 1,
			prompt_text: '# Server instructions for alpha',
			environment: 'linux_ssh',
			ssh: { host: '203.0.113.10', port: 22, user: 'deploy', host_key: '${HOST_KEY}', credential_ref: 'env:MAI_FIXTURE_CREDENTIAL' },
			privilege: 'sudo',
			capabilities: ['shell'],
			file_read_roots: ['/srv/data', '/var/log/app'],
			revision: 1,
			sha256: 'ab'.repeat(32),
			projection: { revision: 1, stale: false }
		};
	}
	const db = { [SERVER_A]: makeProfile(SERVER_A) };
	const calls = [];
	let failNextEdit = null;

	const api = {
		async readProfile(serverId) {
			calls.push({ op: 'readProfile', serverId });
			return structuredClone(db[serverId]);
		},
		async editProfile(serverId, payload) {
			// Mirror the real client transport: the body crosses as JSON, so the
			// recorded payload must be a plain snapshot (proxies never clone).
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'editProfile', serverId, payload: wire });
			if (failNextEdit) {
				const failure = failNextEdit;
				failNextEdit = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const stored = db[serverId];
			// Real boundary behavior (servers.py): stale caller ⇒ value-free 409.
			if (payload.expected_revision !== stored.revision) {
				throw new ApiError(409, 'conflict', '${CONFLICT_MESSAGE}');
			}
			stored.prompt_revision = payload.prompt_revision;
			stored.name = payload.name;
			stored.environment = payload.environment;
			stored.privilege = payload.privilege;
			stored.capabilities = [...payload.capabilities];
			stored.ssh = { ...payload.ssh };
			stored.file_read_roots = [...payload.file_read_roots];
			if (typeof payload.prompt_text === 'string') {
				stored.prompt_text = payload.prompt_text;
			}
			stored.revision += 1;
			stored.sha256 = 'cd'.repeat(32);
			stored.projection = { revision: stored.revision, stale: false };
			return {
				server_id: serverId,
				revision: stored.revision,
				schema_revision: 1,
				sha256: stored.sha256
			};
		}
	};
	const services = serverProfileServices(api);

	// --- registry: the REAL feature entry ------------------------------------
	import { createPanelRegistry } from '${REGISTRY}';
	const registryResult = createPanelRegistry([serverProfileEntry()]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;

	let show = $state(true);

	const descriptor = {
		schema_version: 1,
		window_id: '${WINDOW_A}',
		panel_kind: 'server-profile',
		scope: { kind: 'server', server_id: SERVER_A },
		geometry: { x: 8, y: 8, width: 560, height: 480 },
		presentation: 'normal',
		z: 1,
		title_override: null
	};

	window.__maiRootsApi = {
		calls,
		db,
		failNextEdit(failure) {
			failNextEdit = failure;
		}
	};
</script>

<section data-testid="host-a" aria-label="Host A">
	<ComponentHost label="Host A">
		{#if show}
			<PanelHost {registry} descriptor={descriptor} services={services} />
		{/if}
	</ComponentHost>
</section>
`;

const ENTRY_SOURCE = `
import { mount } from 'svelte';
import App from '${APP_ID}';
import '${TOKENS}';
mount(App, { target: document.getElementById('app') });
`;

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-server-profile-roots-harness',
		resolveId(id) {
			// .svelte virtual modules must NOT get the conventional `\0` prefix:
			// vite-plugin-svelte excludes `\0` ids from Svelte compilation
			// (panel-host harness convention). The JS entry keeps the `\0`.
			if (id === APP_ID) return id;
			if (id === ENTRY_ID) return `\\0${id}`;
			return undefined;
		},
		load(id) {
			if (id === APP_ID) return sources[APP_ID];
			if (id === `\\0${ENTRY_ID}`) return sources[ENTRY_ID];
			return undefined;
		}
	};
}

/** Built lazy chunks (the entry's real dynamic panel import), served by name. */
let chunks: Array<{ fileName: string; code: string }> = [];

test.beforeAll(async () => {
	const sources: Record<string, string> = {
		[APP_ID]: APP_SOURCE,
		[ENTRY_ID]: ENTRY_SOURCE
	};
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(sources)],
		build: {
			write: false,
			emptyOutDir: true,
			cssCodeSplit: false,
			outDir: 'node_modules/.mai-server-profile-roots-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'roots-[hash].js',
					assetFileNames: 'demo[extname]'
				}
			}
		}
	});

	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	chunks = [];
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk' && item.isEntry) harnessJs += item.code;
			else if (item.type === 'chunk') chunks.push({ fileName: item.fileName, code: item.code });
			else if (item.type === 'asset' && item.fileName.endsWith('.css')) {
				harnessCss += String(item.source);
			}
		}
	}
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete: missing JS entry chunk or CSS asset');
	}
	if (chunks.length === 0) {
		throw new Error('harness bundle incomplete: the panel must build as a lazy chunk');
	}
});

test.beforeEach(async ({ page }) => {
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI server-profile-roots fixture</title>
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
		const rest = pathname.slice(FIXTURE_URL.length);
		if (rest === '' || rest.endsWith('/')) {
			return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
		}
		if (rest === 'demo.js') {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (rest === 'demo.css') {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		const chunk = chunks.find((candidate) => candidate.fileName === rest);
		if (chunk) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: chunk.code });
		}
		return route.abort();
	});
});

async function editCalls(page: Page) {
	return page.evaluate(
		() => window.__maiRootsApi!.calls.filter((entry) => entry.op === 'editProfile')
	);
}

test('load renders stored roots as chips and in the read view', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await expect(a.getByTestId('server-id')).toHaveText(SERVER_A);
	await expect(a.getByTestId('edit-file-read-roots-chip')).toHaveCount(2);
	await expect(a.getByTestId('edit-file-read-roots-chip').nth(0)).toContainText('/srv/data');
	await expect(a.getByTestId('edit-file-read-roots-chip').nth(1)).toContainText('/var/log/app');
	await expect(a.getByTestId('edit-file-read-roots-count')).toHaveText('2 / 64');
	await expect(a.getByTestId('edit-file-read-roots-input')).toHaveValue('');
	await expect(a.getByTestId('file-read-roots')).toContainText('/srv/data');
	await expect(a.getByTestId('file-read-roots')).toContainText('/var/log/app');
});

test('adding a valid root shows the chip and the save payload carries it', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await a.getByTestId('edit-file-read-roots-input').fill('/opt/tools');
	await a.getByTestId('edit-file-read-roots-add').click();
	const chips = a.getByTestId('edit-file-read-roots-chip');
	await expect(chips).toHaveCount(3);
	await expect(chips.nth(0)).toContainText('/srv/data');
	await expect(chips.nth(1)).toContainText('/var/log/app');
	await expect(chips.nth(2)).toContainText('/opt/tools');
	await expect(a.getByTestId('edit-file-read-roots-count')).toHaveText('3 / 64');
	await expect(a.getByTestId('edit-file-read-roots-error')).toHaveCount(0);
	await a.getByTestId('save-button').click();
	await expect(a.getByTestId('saved-notice')).toContainText('prompt edition 2');

	const calls = await editCalls(page);
	expect(calls).toHaveLength(1);
	const payload = calls[0].payload as { file_read_roots?: unknown };
	expect(payload.file_read_roots).toEqual(['/srv/data', '/var/log/app', '/opt/tools']);
	const stored = await page.evaluate((id) => window.__maiRootsApi!.db[id], SERVER_A);
	expect(stored.file_read_roots).toEqual(['/srv/data', '/var/log/app', '/opt/tools']);
});

test('removing a root updates the chips and the save payload', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await a.getByTestId('edit-file-read-roots-remove-/var/log/app').click();
	const chips = a.getByTestId('edit-file-read-roots-chip');
	await expect(chips).toHaveCount(1);
	await expect(chips.nth(0)).toContainText('/srv/data');
	await a.getByTestId('save-button').click();
	await expect(a.getByTestId('saved-notice')).toContainText('prompt edition 2');

	const calls = await editCalls(page);
	expect(calls).toHaveLength(1);
	const payload = calls[0].payload as { file_read_roots?: unknown };
	expect(payload.file_read_roots).toEqual(['/srv/data']);
});

test('invalid adds are refused with an inline error, no chip, no save mutation', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await expect(a.getByTestId('edit-file-read-roots-chip')).toHaveCount(2);

	// Relative path.
	await a.getByTestId('edit-file-read-roots-input').fill('srv/data');
	await a.getByTestId('edit-file-read-roots-add').click();
	await expect(a.getByTestId('edit-file-read-roots-error')).toContainText('absolute paths');
	await expect(a.getByTestId('edit-file-read-roots-chip')).toHaveCount(2);

	// The filesystem root itself.
	await a.getByTestId('edit-file-read-roots-input').fill('/');
	await a.getByTestId('edit-file-read-roots-add').click();
	await expect(a.getByTestId('edit-file-read-roots-error')).toContainText('not a valid read root');
	await expect(a.getByTestId('edit-file-read-roots-chip')).toHaveCount(2);

	// Duplicate of an existing chip.
	await a.getByTestId('edit-file-read-roots-input').fill('/srv/data');
	await a.getByTestId('edit-file-read-roots-add').click();
	await expect(a.getByTestId('edit-file-read-roots-error')).toContainText('already in the list');
	await expect(a.getByTestId('edit-file-read-roots-chip')).toHaveCount(2);

	// A refused add mutates nothing: the form is still clean (not dirty), so
	// Save stays blocked and nothing was sent.
	await expect(a.getByTestId('save-button')).toBeDisabled();
	expect(await editCalls(page)).toHaveLength(0);
});

test('empty list shows the fail-closed hint and sends an explicit empty array', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await a.getByTestId('edit-file-read-roots-remove-/srv/data').click();
	await a.getByTestId('edit-file-read-roots-remove-/var/log/app').click();
	const hint = a.getByTestId('edit-file-read-roots-empty');
	await expect(hint).toBeVisible();
	await expect(hint).toContainText('No roots — all file reads are denied (fail closed)');
	await a.getByTestId('save-button').click();
	await expect(a.getByTestId('saved-notice')).toContainText('prompt edition 2');

	const calls = await editCalls(page);
	expect(calls).toHaveLength(1);
	const payload = calls[0].payload as { file_read_roots?: unknown };
	expect(payload.file_read_roots).toEqual([]);
	// Read view falls back to the em dash for no roots.
	await expect(a.getByTestId('file-read-roots')).toHaveText('—');
});

test('a 409 conflict displays the banner, keeps the unsaved roots, never auto-retries', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await a.getByTestId('edit-file-read-roots-input').fill('/opt/unsaved');
	await a.getByTestId('edit-file-read-roots-add').click();
	await expect(a.getByTestId('edit-file-read-roots-chip')).toHaveCount(3);

	await page.evaluate(() =>
		window.__maiRootsApi!.failNextEdit({
			status: 409,
			code: 'conflict',
			message: '${CONFLICT_MESSAGE}'
		})
	);
	await a.getByTestId('save-button').click();
	const banner = a.getByTestId('conflict-banner');
	await expect(banner).toBeVisible();
	await expect(banner).toContainText('changed since this window last loaded');

	// DISPLAYED, not retried: the unsaved roots stay in the form, Save is
	// blocked until an explicit Reload, and exactly ONE edit call happened.
	const chips = a.getByTestId('edit-file-read-roots-chip');
	await expect(chips).toHaveCount(3);
	await expect(chips.nth(2)).toContainText('/opt/unsaved');
	await expect(a.getByTestId('save-button')).toBeDisabled();
	expect(await editCalls(page)).toHaveLength(1);

	// The stored roots were never touched by the refused write.
	const stored = await page.evaluate((id) => window.__maiRootsApi!.db[id], SERVER_A);
	expect(stored.file_read_roots).toEqual(['/srv/data', '/var/log/app']);

	// Explicit Reload is the only recovery: the stored roots return.
	await a.getByTestId('reload-button').click();
	await expect(a.getByTestId('edit-file-read-roots-chip')).toHaveCount(2);
	await expect(a.getByTestId('conflict-banner')).toHaveCount(0);
	await expect(a.getByTestId('edit-file-read-roots-input')).toHaveValue('');
});
