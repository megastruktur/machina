import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-02-web-server — real-browser proofs for the server-profile panel
 * (frontend/src/lib/panels/ServerProfilePanel.svelte) and its feature-owned
 * registry entry (frontend/src/lib/workspace/entries/02-web-server.ts).
 *
 * Focused per the test policy: the panel is exercised in real Chromium,
 * mounted through the reviewed window host (`PanelHost`) and registry with a
 * fixture in-page API implementing the panel's typed service shape (the same
 * interface `createMaiApiClient()` satisfies). The REAL backend boundary —
 * envelope codes, 409 conflict semantics, CSRF/origin — is owned by
 * `02-api-client`/`02-profile-api` tests; the assembled desktop journey
 * (panel beside another panel in the real shell, real page refresh, second
 * browser tab) is owned by `02-verify` at the phase gate.
 *
 * Harness (self-contained, following the ui-controls/panel-host convention):
 *  1. test.beforeAll runs an IN-MEMORY Vite production build (write: false)
 *     of a virtual app that imports the real panel, the real feature entry,
 *     the real registry + PanelHost, and mounts five hosts: two for the SAME
 *     fixture server (stale-edit conflict), one for a second server (target
 *     separation), one benign sibling panel (coexistence/no interference) and
 *     one host with NO services bag (fail-closed).
 *  2. Each test serves the built JS/CSS/HTML through Playwright route
 *     interception. The fixture API records every call on `window` so tests
 *     assert what the panel actually sent — never component internals. A
 *     `toggleHost`/`setPresentation` pair drives real unmount/remount cycles,
 *     which is the component-level form of a page refresh (the in-page
 *     fixture backend intentionally survives it; a real browser reload resets
 *     any in-page fixture by construction, so refresh persistence is proven
 *     against the surviving store — limitation recorded in the evidence).
 *
 * Scenario coverage (brief acceptance; gate scenarios remain 02-verify's):
 *  1. target identity visible, prompt edition editable, no credential
 *     material in the DOM (acceptance 1);
 *  2. save carries `expected_revision`, the saved edition survives a
 *     remount, a stale edit from the second host displays `conflict` and
 *     never overwrites; explicit reload recovers (acceptance 2);
 *  3. registry entry validates: kind registered, server scope allowed, global
 *     scope and object pins rejected, geometry policies declared (acceptance 3);
 *  4. mount reads only; unmount (close) submits nothing; hosts share no state
 *     (acceptance 4: no hidden global target, scoped controller only);
 *  5. C08 rule 6 states: loading, stale projection badge, not-found /
 *     unauthenticated / unavailable errors with retry, save-error banner.
 */

const FIXTURE_URL = '/mai-server-profile-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-server-profile-app.svelte';
const ENTRY_ID = 'virtual:mai-server-profile-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/ServerProfilePanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/02-web-server.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const WINDOW_A = '1a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d';
const WINDOW_A2 = '2b2c3d4e-5f6a-4b7c-9d8e-1f2a3b4c5d6e';
const WINDOW_B = '3c3d4e5f-6a7b-4c8d-8e9f-2a3b4c5d6e7f';
const WINDOW_SIBLING = '4d4e5f6a-7b8c-4d9e-9f8a-3b4c5d6e7f8a';
const WINDOW_BARE = '5e5f6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9b';
const RUN_PIN = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';

/**
 * Secret-SHAPED fixture markers (synthetic values): the profile API returns
 * them as plain fields, and the panel's display policy must exclude
 * credential metadata entirely (brief acceptance 1).
 */
const CREDENTIAL_REF = 'env:MAI_FIXTURE_CREDENTIAL';
/** Synthetic pinned fingerprint (SHA256: form, as enrollment stores it). */
const HOST_KEY = 'SHA256:F1xTur3F1nGeRPr1nT0000000000000000000000000';

/** The frozen stale-edit conflict message of the profile API (servers.py). */
const CONFLICT_MESSAGE = 'the profile changed since the last observed revision';

type ReadFailure = { status: number; code: string; message: string };

interface WindowApi {
	calls: Array<{ op: string; serverId: string; payload?: unknown }>;
	db: Record<string, Record<string, unknown>>;
	armReadGate(): void;
	releaseReadGate(): void;
	failNextRead(failure: ReadFailure | null): void;
	failNextEdit(failure: ReadFailure | null): void;
	setStale(serverId: string, stale: boolean): void;
	setPresentation(windowId: 'A' | 'A2', presentation: 'normal' | 'minimized'): void;
	toggleHost(host: 'A' | 'A2'): void;
	registry: {
		kinds: string[];
		checkScope(kind: string, scope: unknown): { ok: true } | { ok: false; code: string };
		defaultGeometry(kind: string): unknown;
		minGeometry(kind: string): unknown;
	};
}

declare global {
	interface Window {
		__maiServerProfileApi?: WindowApi;
		/** Set by a test init script: arm the read gate at app boot. */
		__maiArmGateOnBoot?: boolean;
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
 * The fixture app. Virtual .svelte modules compile in RUNES mode in this
 * in-memory build (observed convention from panel-host.spec.ts), so the app
 * uses `$state`/`$derived` only. The fixture API mirrors the REAL profile API
 * behavior the panel depends on: the `expected_revision` pre-check ⇒
 * value-free 409 `conflict` (servers.py), C01 envelope codes via the real
 * ApiError class from the integrated client.
 */
const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { ApiError } from '${CLIENT}';
	import { serverProfileEntry, serverProfileServices } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';

	// --- fixture backend: the only writer; tests read it through window ------
	function makeProfile(serverId, edition, revision) {
		return {
			schema_version: 1,
			server_id: serverId,
			name: serverId === SERVER_A ? 'alpha' : 'beta',
			prompt_revision: edition,
			prompt_text: '# Server instructions for ' + (serverId === SERVER_A ? 'alpha' : 'beta'),
			environment: 'linux_ssh',
			ssh: { host: '203.0.113.10', port: 22, user: 'deploy', host_key: '${HOST_KEY}', credential_ref: '${CREDENTIAL_REF}' },
			privilege: 'sudo',
			capabilities: ['shell', 'package'],
			revision,
			sha256: 'ab'.repeat(32),
			projection: { revision, stale: false }
		};
	}
	const db = {
		[SERVER_A]: makeProfile(SERVER_A, 1, 1),
		[SERVER_B]: makeProfile(SERVER_B, 4, 7)
	};
	const calls = [];
	let failNextRead = null;
	let failNextEdit = null;
	let readGate = null;

	const api = {
		async readProfile(serverId) {
			calls.push({ op: 'readProfile', serverId });
			if (failNextRead) {
				const failure = failNextRead;
				failNextRead = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			if (readGate) await readGate.promise;
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

	// --- registry: the REAL feature entry + one benign sibling ----------------
	const sibling = createRawSnippet(() => {
		return {
			render: () =>
				'<div class="mai-sibling" data-testid="sibling-panel">' +
				'<p>Sibling fixture panel — benign, no domain data.</p>' +
				'<button type="button" data-testid="sibling-interactions">Sibling interactions: 0</button>' +
				'</div>',
			setup: (root) => {
				const button = root.querySelector('button');
				let count = 0;
				const onClick = () => {
					count += 1;
					button.textContent = 'Sibling interactions: ' + count;
				};
				button.addEventListener('click', onClick);
				return () => button.removeEventListener('click', onClick);
			}
		};
	});
	const registryResult = createPanelRegistry([
		serverProfileEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;

	// --- hosts: A + A2 on SERVER_A (conflict), B on SERVER_B, sibling, bare ---
	let presentationA = $state('normal');
	let presentationA2 = $state('normal');
	let showA = $state(true);
	let showA2 = $state(true);

	function descriptor(windowId, serverId, presentation, z) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: 'server-profile',
			scope: { kind: 'server', server_id: serverId },
			geometry: { x: 8, y: 8, width: 560, height: 480 },
			presentation,
			z,
			title_override: null
		};
	}
	const descriptorSibling = {
		schema_version: 1,
		window_id: '${WINDOW_SIBLING}',
		panel_kind: 'fixture-notes',
		scope: { kind: 'server', server_id: SERVER_B },
		geometry: { x: 16, y: 16, width: 320, height: 240 },
		presentation: 'normal',
		z: 4,
		title_override: null
	};

	const windowApi = {
		calls,
		db,
		armReadGate() {
			const { promise, resolve } = Promise.withResolvers();
			readGate = { promise, resolve };
		},
		releaseReadGate() {
			if (readGate) {
				readGate.resolve();
				readGate = null;
			}
		},
		failNextRead(failure) {
			failNextRead = failure;
		},
		failNextEdit(failure) {
			failNextEdit = failure;
		},
		setStale(serverId, stale) {
			db[serverId].projection = { revision: db[serverId].projection.revision, stale };
		},
		setPresentation(windowId, presentation) {
			if (windowId === 'A') presentationA = presentation;
			else presentationA2 = presentation;
		},
		toggleHost(host) {
			if (host === 'A') showA = !showA;
			else showA2 = !showA2;
		},
		registry: {
			kinds: Array.from(registry.knownPanelKinds()).sort(),
			checkScope(kind, scope) {
				const result = registry.checkScope(kind, scope);
				return result.ok ? { ok: true } : { ok: false, code: result.issue.code };
			},
			defaultGeometry(kind) {
				return registry.defaultGeometry(kind);
			},
			minGeometry(kind) {
				return registry.minGeometry(kind);
			}
		}
	};
	window.__maiServerProfileApi = windowApi;
	if (window.__maiArmGateOnBoot === true) windowApi.armReadGate();
</script>

<section data-testid="host-a" aria-label="Host A">
	<ComponentHost label="Host A">
		{#if showA}
			<PanelHost {registry} descriptor={descriptor('${WINDOW_A}', SERVER_A, presentationA, 1)} services={services} />
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-a2" aria-label="Host A2">
	<ComponentHost label="Host A2">
		{#if showA2}
			<PanelHost {registry} descriptor={descriptor('${WINDOW_A2}', SERVER_A, presentationA2, 2)} services={services} />
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-b" aria-label="Host B">
	<ComponentHost label="Host B">
		<PanelHost {registry} descriptor={descriptor('${WINDOW_B}', SERVER_B, 'normal', 3)} services={services} />
	</ComponentHost>
</section>
<section data-testid="host-sibling" aria-label="Host sibling">
	<ComponentHost label="Host sibling">
		<PanelHost {registry} descriptor={descriptorSibling} />
	</ComponentHost>
</section>
<section data-testid="host-bare" aria-label="Host bare">
	<ComponentHost label="Host bare">
		<PanelHost {registry} descriptor={descriptor('${WINDOW_BARE}', SERVER_A, 'normal', 5)} />
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
		name: 'mai-server-profile-harness',
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
/** Chunk names actually fetched by the browser (lazy-loader proof). */
let servedChunks: string[] = [];

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
			outDir: 'node_modules/.mai-server-profile-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'profile-[hash].js',
					assetFileNames: 'demo[extname]'
				}
			}
		}
	});

	// `build()` is one-shot (no watcher configured); structural boundary type.
	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	chunks = [];
	harnessJs = '';
	harnessCss = '';
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') {
				if (item.isEntry) harnessJs += item.code;
				else chunks.push({ fileName: item.fileName, code: item.code });
			} else if (item.fileName.endsWith('.css')) {
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
	servedChunks = [];
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI server-profile fixture</title>
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
			servedChunks.push(rest);
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: chunk.code });
		}
		return route.abort();
	});
});

test('renders the pinned target identity, editable profile fields & prompt textarea, no credential material', async ({
page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await page.goto(FIXTURE_URL);

	const a = page.getByTestId('host-a');
	await expect(a.getByTestId('server-id')).toHaveText(SERVER_A);
	await expect(a.getByTestId('profile-name')).toHaveText('alpha');
	await expect(a.getByTestId('ssh-target')).toHaveText('deploy@203.0.113.10:22');
	await expect(a.getByTestId('privilege')).toHaveText('sudo');
	await expect(a.getByTestId('profile-revision')).toHaveText('1');
	await expect(a.getByTestId('prompt-edition-badge')).toHaveText('edition 1');

	// Acceptance 1: profile fields + server system prompt + capability chips/dropdown are editable.
	await expect(a.getByTestId('edit-name')).toHaveValue('alpha');
	await expect(a.getByTestId('edit-ssh-host')).toHaveValue('203.0.113.10');
	await expect(a.getByTestId('edit-ssh-port')).toHaveValue('22');
	await expect(a.getByTestId('edit-ssh-user')).toHaveValue('deploy');
	await expect(a.getByTestId('edit-capabilities-chip')).toHaveCount(2);
	await expect(a.getByTestId('prompt-text-input')).toHaveValue('# Server instructions for alpha');

	// so-03: the stored host key fingerprint is displayed READ-ONLY as
	// `SSH host key (pinned)`; the rotation and credential inputs are gone.
	const pinned = a.getByTestId('pinned-host-key');
	await expect(pinned).toHaveValue(HOST_KEY);
	await expect(pinned).toHaveAttribute('readonly', '');
	await expect(a.getByText('SSH host key (pinned)')).toBeVisible();
	await expect(a.getByTestId('edit-host-key')).toHaveCount(0);
	await expect(a.getByTestId('edit-credential-key')).toHaveCount(0);
	await expect(a.getByText('Rotate credential env key')).toHaveCount(0);

	// Acceptance 1: no credential material anywhere in the rendered document.
	const content = await page.content();
	expect(content).not.toContain(CREDENTIAL_REF);
	expect(content).not.toContain('credential_ref');
	await expect(page.locator('input[type="password"]')).toHaveCount(0);

	// Acceptance 3: the component arrived through the entry's real lazy loader.
	expect(servedChunks.length).toBeGreaterThan(0);

	expect(errors).toEqual([]);
});

test('save posts expected_revision and auto-bumps the prompt edition; the saved edition survives a remount (refresh)', async ({
page
}) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await expect(a.getByTestId('prompt-edition-badge')).toHaveText('edition 1');

	// Edit display name + capabilities (remove 'package', add 'host.shell') + system prompt.
	await a.getByTestId('edit-name').fill('alpha-renamed');
	await a.getByTestId('edit-capabilities-remove-package').click();
	await a.getByTestId('edit-capabilities-select').selectOption('host.shell');
	await a.getByTestId('prompt-text-input').fill('# Alpha runbook\nKeep containers under 1 GiB.');
	await a.getByTestId('save-button').click();
	await expect(a.getByTestId('saved-notice')).toContainText('prompt edition 2');

	// The write carried the observed revision for the stale-caller guard.
	const [call] = await page.evaluate(() =>
		window.__maiServerProfileApi!.calls.filter((entry) => entry.op === 'editProfile')
	);
	expect(call.serverId).toBe(SERVER_A);
	const payload = call.payload as {
		prompt_revision: number;
		prompt_text: string;
		expected_revision: number;
		name: string;
		capabilities: string[];
		ssh: { host_key: string; credential_ref: string };
	};
	expect(payload.prompt_revision).toBe(2); // auto-bumped 1 → 2
	expect(payload.prompt_text).toBe('# Alpha runbook\nKeep containers under 1 GiB.');
	expect(payload.expected_revision).toBe(1);
	expect(payload.name).toBe('alpha-renamed');
	expect(payload.capabilities).toEqual(['host.shell', 'shell']);
	// so-03: ordinary edits submit the STORED trust fields unchanged — the
	// pinned host key and credential reference can never drift via this panel.
	expect(payload.ssh.host_key).toBe(HOST_KEY);
	expect(payload.ssh.credential_ref).toBe(CREDENTIAL_REF);

	// Post-commit triple shown; the silent re-read confirms the record.
	await expect(a.getByTestId('profile-revision')).toHaveText('2');
	await expect(a.getByTestId('prompt-edition-badge')).toHaveText('edition 2');
	await expect(a.getByTestId('edit-name')).toHaveValue('alpha-renamed');
	await expect(a.getByTestId('prompt-text-input')).toHaveValue('# Alpha runbook\nKeep containers under 1 GiB.');

	// Component-level refresh: unmount (close) + remount re-reads the store.
	await page.evaluate(() => window.__maiServerProfileApi!.toggleHost('A'));
	await expect(a.getByTestId('server-profile-root')).toHaveCount(0);
	await page.evaluate(() => window.__maiServerProfileApi!.toggleHost('A'));
	const remounted = page.getByTestId('host-a');
	await expect(remounted.getByTestId('prompt-edition-badge')).toHaveText('edition 2');
	await expect(remounted.getByTestId('edit-name')).toHaveValue('alpha-renamed');
	await expect(remounted.getByTestId('profile-revision')).toHaveText('2');

	const stored = await page.evaluate(
		(id) => window.__maiServerProfileApi!.db[id],
		SERVER_A
	);
	expect(stored.prompt_revision).toBe(2);
	expect(stored.revision).toBe(2);
});

test('stale second editor displays the conflict and never overwrites; reload recovers', async ({
page
}) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	const a2 = page.getByTestId('host-a2');
	await expect(a.getByTestId('profile-revision')).toHaveText('1');
	await expect(a2.getByTestId('profile-revision')).toHaveText('1');

	// First editor saves prompt change (observed revision 1 → committed 2, edition auto-bumped 1 → 2).
	await a2.getByTestId('prompt-text-input').fill('# A2 runbook');
	await a2.getByTestId('save-button').click();
	await expect(a2.getByTestId('saved-notice')).toContainText('prompt edition 2');

	// Second editor (still observing revision 1) saves its own prompt ⇒ 409 conflict.
	await a.getByTestId('prompt-text-input').fill('# A runbook');
	await a.getByTestId('save-button').click();
	const banner = a.getByTestId('conflict-banner');
	await expect(banner).toBeVisible();
	await expect(banner).toContainText('changed since this window last loaded');

	// DISPLAYED, not overwritten: the stored profile is untouched, the unsaved
	// input is kept, Save stays blocked, and no retry fired without user action.
	const stored = await page.evaluate((id) => window.__maiServerProfileApi!.db[id], SERVER_A);
	expect(stored.prompt_revision).toBe(2);
	expect(stored.revision).toBe(2);
	await expect(a.getByTestId('prompt-text-input')).toHaveValue('# A runbook');
	await expect(a.getByTestId('save-button')).toBeDisabled();
	await expect(a.getByTestId('profile-error')).toHaveCount(0);
	const editCallCount = await page.evaluate(
		() => window.__maiServerProfileApi!.calls.filter((entry) => entry.op === 'editProfile').length
	);
	expect(editCallCount).toBe(2); // A2's success + A's rejected attempt

	// The explicit reload is the only path back to editing.
	await a.getByTestId('reload-button').click();
	await expect(a.getByTestId('prompt-text-input')).toHaveValue('# A2 runbook');
	await expect(a.getByTestId('profile-revision')).toHaveText('2');
	await a.getByTestId('prompt-text-input').fill('# A runbook final');
	await a.getByTestId('save-button').click();
	await expect(a.getByTestId('saved-notice')).toContainText('prompt edition 3');
	const final = await page.evaluate((id) => window.__maiServerProfileApi!.db[id], SERVER_A);
	expect(final.prompt_revision).toBe(3);
	expect(final.revision).toBe(3);
});

test('mount reads only; unmount (close) submits nothing; remount shows the authoritative record', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await expect(a.getByTestId('profile-revision')).toHaveText('1');

	// Three profile hosts read once each (A, A2, B); zero writes on mount.
	const ops = await page.evaluate(() =>
		window.__maiServerProfileApi!.calls.map((entry) => entry.op)
	);
	expect(ops.filter((op) => op === 'readProfile')).toHaveLength(3);
	expect(ops.filter((op) => op === 'editProfile')).toHaveLength(0);

	// Local dirty edits + close (content unmount) submit nothing.
	await a.getByTestId('prompt-text-input').fill('# Unsaved local draft');
	await page.evaluate(() => window.__maiServerProfileApi!.setPresentation('A', 'minimized'));
	await expect(a.getByTestId('server-profile-root')).toHaveCount(0);
	const opsAfterClose = await page.evaluate(() =>
		window.__maiServerProfileApi!.calls.map((entry) => entry.op)
	);
	expect(opsAfterClose.filter((op) => op === 'editProfile')).toHaveLength(0);
	const stored = await page.evaluate((id) => window.__maiServerProfileApi!.db[id], SERVER_A);
	expect(stored.prompt_revision).toBe(1);

	// Reopening re-reads the authoritative record; local edits are gone.
	await page.evaluate(() => window.__maiServerProfileApi!.setPresentation('A', 'normal'));
	await expect(a.getByTestId('profile-revision')).toHaveText('1');
	await expect(a.getByTestId('prompt-text-input')).toHaveValue('# Server instructions for alpha');
});

test('two pinned servers never cross; sibling interaction drives no traffic', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	const b = page.getByTestId('host-b');
	await expect(a.getByTestId('server-id')).toHaveText(SERVER_A);
	await expect(b.getByTestId('server-id')).toHaveText(SERVER_B);
	await expect(b.getByTestId('profile-revision')).toHaveText('7');
	await expect(page.getByTestId('host-sibling').getByTestId('sibling-panel')).toBeVisible();

	// Saving in A targets ONLY A; B's displayed record is untouched.
	await a.getByTestId('prompt-text-input').fill('# Alpha only');
	await a.getByTestId('save-button').click();
	await expect(a.getByTestId('saved-notice')).toContainText('prompt edition 2');
	await expect(b.getByTestId('profile-revision')).toHaveText('7');
	const editCalls = await page.evaluate(() =>
		window.__maiServerProfileApi!.calls.filter((entry) => entry.op === 'editProfile')
	);
	expect(editCalls).toHaveLength(1);
	expect(editCalls[0].serverId).toBe(SERVER_A);
	const beta = await page.evaluate((id) => window.__maiServerProfileApi!.db[id], SERVER_B);
	expect(beta.prompt_revision).toBe(4);

	// Interacting with the sibling panel changes nothing on the wire.
	const callsBefore = await page.evaluate(() => window.__maiServerProfileApi!.calls.length);
	const siblingButton = page.getByTestId('host-sibling').getByTestId('sibling-interactions');
	await siblingButton.click();
	await expect(siblingButton).toHaveText('Sibling interactions: 1');
	expect(await page.evaluate(() => window.__maiServerProfileApi!.calls.length)).toBe(callsBefore);
});

test('a stale projection is displayed, never hidden (C08 rule 6)', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await expect(a.getByTestId('profile-revision')).toHaveText('1');
	await expect(a.getByTestId('projection-stale')).toHaveCount(0);

	await page.evaluate((id) => window.__maiServerProfileApi!.setStale(id, true), SERVER_A);
	await page.evaluate(() => window.__maiServerProfileApi!.setPresentation('A', 'minimized'));
	await page.evaluate(() => window.__maiServerProfileApi!.setPresentation('A', 'normal'));
	await expect(a.getByTestId('projection-stale')).toBeVisible();
	await expect(page.getByTestId('host-b').getByTestId('projection-stale')).toHaveCount(0);
});

test('failure states: not-found, unauthenticated, save failure; retry recovers', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await expect(a.getByTestId('profile-revision')).toHaveText('1');

	// 404 not_found on a fresh mount ⇒ safe unavailable state (A only).
	await page.evaluate(() =>
		window.__maiServerProfileApi!.failNextRead({
			status: 404,
			code: 'not_found',
			message: 'no canonical profile for this server id'
		})
	);
	await page.evaluate(() => window.__maiServerProfileApi!.setPresentation('A', 'minimized'));
	await page.evaluate(() => window.__maiServerProfileApi!.setPresentation('A', 'normal'));
	const error = a.getByTestId('profile-error');
	await expect(error).toBeVisible();
	await expect(error).toHaveAttribute('data-error-code', 'not_found');
	await expect(error).toContainText('does not exist');
	// Sibling host unaffected (error containment).
	await expect(page.getByTestId('host-a2').getByTestId('profile-revision')).toHaveText('1');

	// Retry after the fault clears re-reads successfully.
	await a.getByTestId('profile-retry').click();
	await expect(a.getByTestId('profile-revision')).toHaveText('1');

	// Unauthenticated maps to the session message (no auto re-login loop).
	await page.evaluate(() =>
		window.__maiServerProfileApi!.failNextRead({
			status: 401,
			code: 'unauthenticated',
			message: 'no valid session'
		})
	);
	await page.evaluate(() => window.__maiServerProfileApi!.setPresentation('A', 'minimized'));
	await page.evaluate(() => window.__maiServerProfileApi!.setPresentation('A', 'normal'));
	await expect(a.getByTestId('profile-error')).toContainText('session has ended');

	// Retry recovers here too.
	await a.getByTestId('profile-retry').click();
	await expect(a.getByTestId('profile-revision')).toHaveText('1');

	// Save-time non-conflict failure: banner keeps the editor open; retry saves.
	await a.getByTestId('prompt-text-input').fill('# Retry prompt');
	await page.evaluate(() =>
		window.__maiServerProfileApi!.failNextEdit({
			status: 503,
			code: 'unavailable',
			message: 'upstream unavailable'
		})
	);
	await a.getByTestId('save-button').click();
	const banner = a.getByTestId('save-error-banner');
	await expect(banner).toBeVisible();
	await expect(banner).toHaveAttribute('data-error-code', 'unavailable');
	await expect(a.getByTestId('profile-revision')).toHaveText('1'); // nothing written
	await a.getByTestId('save-button').click(); // fault consumed; retry succeeds
	await expect(a.getByTestId('saved-notice')).toContainText('prompt edition 2');
});

test('loading state holds until the profile arrives (C08 rule 6)', async ({ page }) => {
	await page.addInitScript(() => {
		window.__maiArmGateOnBoot = true;
	});
	await page.goto(FIXTURE_URL);
	const a = page.getByTestId('host-a');
	await expect(a.getByTestId('profile-loading')).toBeVisible();
	await page.evaluate(() => window.__maiServerProfileApi!.releaseReadGate());
	await expect(a.getByTestId('profile-revision')).toHaveText('1');
});

test('feature entry validates in the real registry (scope policy and geometry)', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const probe = await page.evaluate((pin) => {
		const registry = window.__maiServerProfileApi!.registry;
		return {
			kinds: registry.kinds,
			serverScope: registry.checkScope('server-profile', {
				kind: 'server',
				server_id: pin.serverA
			}),
			globalScope: registry.checkScope('server-profile', { kind: 'global' }),
			objectPin: registry.checkScope('server-profile', {
				kind: 'server',
				server_id: pin.serverA,
				object: { kind: 'run', id: pin.runId }
			}),
			defaultGeometry: registry.defaultGeometry('server-profile'),
			minGeometry: registry.minGeometry('server-profile')
		};
	}, { serverA: SERVER_A, runId: RUN_PIN });

	expect(probe.kinds).toContain('server-profile');
	expect(probe.serverScope).toEqual({ ok: true });
	expect(probe.globalScope).toEqual({ ok: false, code: 'scope_kind_not_allowed' });
	expect(probe.objectPin).toEqual({ ok: false, code: 'scope_object_kind_not_allowed' });
	expect(probe.defaultGeometry).toEqual({ x: 96, y: 64, width: 560, height: 420 });
	expect(probe.minGeometry).toEqual({ x: 0, y: 0, width: 380, height: 280 });
});

test('missing services bag fails closed without issuing requests', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	await expect(page.getByTestId('host-bare').getByTestId('panel-config-error')).toBeVisible();
	const readCount = await page.evaluate(
		() => window.__maiServerProfileApi!.calls.filter((entry) => entry.op === 'readProfile').length
	);
	// A, A2 and B each read once; the bare host (no services) issued nothing.
	expect(readCount).toBe(3);
});
