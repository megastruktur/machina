import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-03-web-policy — real-browser proofs for the capability-policy panel
 * (frontend/src/lib/panels/CapabilityPolicyPanel.svelte) and its feature-owned
 * registry entry (frontend/src/lib/workspace/entries/03-web-policy.ts).
 *
 * Focused per the test policy: the panel is exercised in real Chromium,
 * mounted through the reviewed window host (`PanelHost`) and registry with a
 * fixture in-page API implementing the panel's typed service shape (mirroring
 * the reviewed P03 producers `mai.api.grants`/`mai.api.approvals`: the
 * `expected_revision` pre-check ⇒ value-free 409 `conflict`, the one-use
 * decision precedence ⇒ stable denial codes). The REAL backend boundary —
 * envelope codes, CSRF/origin, SQL row-lock race — is owned by
 * `03-grants-api`/`03-approval-api` tests; the assembled desktop journey
 * (panel beside another panel in the real shell, real page refresh) is owned
 * by `03-verify` at the phase gate.
 *
 * Harness (self-contained, following the ui-controls/panel-host convention):
 *  1. test.beforeAll runs an IN-MEMORY Vite production build (write: false)
 *     of a virtual app that imports the real panel, the real feature entry,
 *     the real registry + PanelHost, and mounts five hosts: two global policy
 *     panels (stale-edit conflict), one benign sibling panel (coexistence),
 *     one host with NO services bag and one server-scoped host (both
 *     fail-closed).
 *  2. Each test serves the built JS/CSS/HTML through Playwright route
 *     interception. The fixture API records every call on `window` so tests
 *     assert what the panel actually sent — never component internals.
 *     `toggleHost` drives real unmount/remount cycles (the component-level
 *     form of a page refresh; limitation recorded in the evidence).
 *
 * Scenario coverage (brief acceptance; the gate scenario stays 03-verify's):
 *  1. enabled tools (grants/deny) and pending-operation decisions (ask) are
 *     DISTINCT visible control groups; approvals show the exact target and
 *     the safe binding preview; no secret material in the DOM (acceptance 1);
 *  2. grant revocation commits a versioned edit; a stale editor displays the
 *     conflict and never overwrites; permission revocation and approval
 *     denials (replay/race, stale revision) are visible; no optimistic
 *     executed-success before the result (acceptance 2);
 *  3. the entry validates in the real registry: global-only scope, singleton
 *     key, declared geometry policies (acceptance 3);
 *  4. mount reads only; unmount submits nothing; fail-closed without
 *     services; sibling interaction drives no traffic; no shared state
 *     between hosts (acceptance 4).
 */

const FIXTURE_URL = '/mai-policy-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-policy-app.svelte';
const ENTRY_ID = 'virtual:mai-policy-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/CapabilityPolicyPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/03-web-policy.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const APPROVAL_PENDING = 'c1d2e3f4-a5b6-4c7d-8e9f-1a2b3c4d5e6f';
const APPROVAL_RACE = 'd2e3f4a5-b6c7-4d8e-9f0a-2b3c4d5e6f7a';
const WINDOW_G = '6f6f6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_G2 = '7a7a6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_SIBLING = '4d4e5f6a-7b8c-4d9e-9f8a-3b4c5d6e7f8a';
const WINDOW_BARE = '5e5f6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9b';
const WINDOW_SV = '8b8b6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9e';

/** The value-free conflict body of the grants API (C01 rule 3). */
const CONFLICT_MESSAGE = 'the configuration changed since the last observed revision';
/** Session-shaped principals (hashed session identities, never tokens). */
const SESSION_PRINCIPAL = `sesh-${'aa'.repeat(16)}`;
const OTHER_PRINCIPAL = `sesh-${'bb'.repeat(16)}`;

type ReadFailure = { status: number; code: string; message: string };

interface PolicyWindowApi {
	calls: Array<{ op: string; approvalId?: string; payload?: unknown }>;
	config: Record<string, unknown> | null;
	approvals: Record<string, Record<string, unknown>>;
	armReadGate(): void;
	releaseReadGate(): void;
	failNextRead(failure: ReadFailure | null): void;
	failNextEdit(failure: ReadFailure | null): void;
	failNextReadApproval(failure: ReadFailure | null): void;
	failNextDecide(failure: ReadFailure | null): void;
	consumeExternally(approvalId: string): void;
	deleteConfig(): void;
	toggleHost(host: 'G' | 'G2'): void;
	registry: {
		kinds: string[];
		scopeChecks: { global: string; serverScope: string; unknownKind: string };
		defaultGeometry: unknown;
		minGeometry: unknown;
		instanceKey: string | null;
	};
}

declare global {
	interface Window {
		__maiPolicyApi?: PolicyWindowApi;
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
 * in-memory build (observed convention from panel-host.spec.ts). The fixture
 * API mirrors the REAL producer behavior the panel depends on: the
 * `expected_revision` pre-check ⇒ value-free 409 `conflict` (grants.py) and
 * the one-use decision precedence with stable denial codes (approvals.py):
 * not_found → already_consumed → expired → capability_mismatch →
 * target_mismatch → payload_mismatch → revision_mismatch; a denial never
 * mutates the row.
 */
const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry, definePanelEntry, singletonInstanceKey } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { ApiError } from '${CLIENT}';
	import {
		capabilityPolicyEntry,
		capabilityPolicyServices,
		CAPABILITY_POLICY_PANEL_KIND
	} from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const APPROVAL_PENDING = '${APPROVAL_PENDING}';
	const APPROVAL_RACE = '${APPROVAL_RACE}';
	const SESSION_PRINCIPAL = '${SESSION_PRINCIPAL}';
	const OTHER_PRINCIPAL = '${OTHER_PRINCIPAL}';
	const CONFLICT_MESSAGE = '${CONFLICT_MESSAGE}';

	// Deterministic document hash per revision (shape-only fixture).
	function shaFor(revision) {
		return revision.toString(16).padStart(2, '0').repeat(32);
	}

	// --- fixture backend: the only writer; tests read it through window ------
	let config = {
		revision: 1,
		schema_revision: 1,
		sha256: shaFor(1),
		policy: { revision: 1, denied_capabilities: ['shell.raw'] },
		grants: [
			{ capability_id: 'pkg.install', server_ids: [SERVER_A, SERVER_B] },
			{ capability_id: 'docs.reindex', server_ids: [SERVER_B] },
			{ capability_id: 'shell.raw', server_ids: [SERVER_A] }
		]
	};
	const approvals = {
		[APPROVAL_PENDING]: {
			approval_id: APPROVAL_PENDING,
			capability_id: 'pkg.install',
			server_id: SERVER_A,
			payload_hash: 'cd'.repeat(32),
			policy_revision: 1,
			status: 'pending',
			created_by: SESSION_PRINCIPAL,
			created_at: '2026-10-14T09:00:00+00:00',
			expires_at: '2026-10-14T09:05:00+00:00',
			consumed_by: null,
			consumed_at: null
		},
		[APPROVAL_RACE]: {
			approval_id: APPROVAL_RACE,
			capability_id: 'docs.reindex',
			server_id: SERVER_B,
			payload_hash: 'ef'.repeat(32),
			policy_revision: 1,
			status: 'pending',
			created_by: OTHER_PRINCIPAL,
			created_at: '2026-10-14T09:00:00+00:00',
			expires_at: '2026-10-14T09:05:00+00:00',
			consumed_by: null,
			consumed_at: null
		}
	};
	const calls = [];
	let failNextRead = null;
	let failNextEdit = null;
	let failNextReadApproval = null;
	let failNextDecide = null;
	let readGate = null;

	const api = {
		async readGrants() {
			calls.push({ op: 'readGrants' });
			if (failNextRead) {
				const failure = failNextRead;
				failNextRead = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			if (readGate) await readGate.promise;
			// Real boundary behavior (grants.py): before the first commit the
			// read answers 404 not_found — never a faked empty default.
			if (!config) throw new ApiError(404, 'not_found', 'no committed configuration');
			return structuredClone(config);
		},
		async editGrants(payload) {
			// Mirror the real client transport: the body crosses as JSON, so the
			// recorded payload must be a plain snapshot (proxies never clone).
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'editGrants', payload: wire });
			if (failNextEdit) {
				const failure = failNextEdit;
				failNextEdit = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			// Real boundary behavior (grants.py): stale caller ⇒ value-free 409;
			// the first commit starts at revision 1 from expected_revision 0.
			const baseRevision = config?.revision ?? 0;
			if (payload.expected_revision !== baseRevision) {
				throw new ApiError(409, 'conflict', CONFLICT_MESSAGE);
			}
			const revision = payload.expected_revision + 1;
			config = {
				revision,
				schema_revision: 1,
				sha256: shaFor(revision),
				policy: {
					revision,
					denied_capabilities: [...(payload.denied_capabilities ?? [])]
				},
				grants: (payload.grants ?? []).map((entry) => ({
					capability_id: entry.capability_id,
					server_ids: [...entry.server_ids]
				}))
			};
			return { revision: config.revision, schema_revision: 1, sha256: config.sha256 };
		},
		async readApproval(approvalId) {
			calls.push({ op: 'readApproval', approvalId });
			if (failNextReadApproval) {
				const failure = failNextReadApproval;
				failNextReadApproval = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const row = approvals[approvalId];
			if (!row) throw new ApiError(404, 'not_found', 'no approval record for this id');
			return { ...row };
		},
		async decideApproval(approvalId, payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'decideApproval', approvalId, payload: wire });
			if (failNextDecide) {
				const failure = failNextDecide;
				failNextDecide = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const row = approvals[approvalId];
			// Frozen denial precedence (approvals.py); a denial never mutates.
			if (!row) throw new ApiError(404, 'not_found', 'no approval record for this id');
			if (row.status !== 'pending') {
				throw new ApiError(409, 'already_consumed', 'decision already consumed (one-use)');
			}
			if (payload.capability_id !== row.capability_id) {
				throw new ApiError(409, 'capability_mismatch', 'presented capability does not match');
			}
			if (payload.server_id !== row.server_id) {
				throw new ApiError(409, 'target_mismatch', 'presented target does not match');
			}
			if (payload.payload_hash !== row.payload_hash) {
				throw new ApiError(409, 'payload_mismatch', 'presented payload hash does not match');
			}
			if (payload.policy_revision !== row.policy_revision) {
				throw new ApiError(
					409,
					'revision_mismatch',
					'presented policy revision does not match the approval binding'
				);
			}
			row.status = 'consumed';
			row.consumed_by = OTHER_PRINCIPAL;
			row.consumed_at = '2026-10-14T09:01:00+00:00';
			return { ...row };
		}
	};
	const services = capabilityPolicyServices(api);

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
		capabilityPolicyEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;
	const entryCheck = definePanelEntry(capabilityPolicyEntry());
	if (!entryCheck.ok) throw new Error('feature entry rejected: ' + entryCheck.issue.code);

	// --- hosts: G + G2 global (conflict), sibling, bare, server-scoped --------
	let showG = $state(true);
	let showG2 = $state(true);

	function descriptor(windowId, scope, z) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: 'capability-policy',
			scope,
			geometry: { x: 8, y: 8, width: 640, height: 480 },
			presentation: 'normal',
			z,
			title_override: null
		};
	}
	const GLOBAL = { kind: 'global' };
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
		// Live view of the (possibly deleted) document through a getter.
		get config() {
			return config;
		},
		approvals,
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
		failNextReadApproval(failure) {
			failNextReadApproval = failure;
		},
		failNextDecide(failure) {
			failNextDecide = failure;
		},
		consumeExternally(approvalId) {
			// The one-use UPDATE won in the OTHER admin session first.
			const row = approvals[approvalId];
			row.status = 'consumed';
			row.consumed_by = OTHER_PRINCIPAL;
			row.consumed_at = '2026-10-14T09:02:00+00:00';
		},
		deleteConfig() {
			// Simulates a data root without a committed document: reads answer
			// 404 not_found and the first edit starts at revision 1.
			config = null;
		},
		toggleHost(host) {
			if (host === 'G') showG = !showG;
			else showG2 = !showG2;
		},
		registry: (() => {
			// Precomputed PLAIN results: page.evaluate serializes, so live
			// functions never cross to the test.
			function checkCode(kind, scope) {
				const result = registry.checkScope(kind, scope);
				return result.ok ? 'ok' : result.issue.code;
			}
			return {
				kinds: Array.from(registry.knownPanelKinds()).sort(),
				scopeChecks: {
					global: checkCode('capability-policy', { kind: 'global' }),
					serverScope: checkCode('capability-policy', {
						kind: 'server',
						server_id: SERVER_A
					}),
					unknownKind: checkCode('not-a-panel', { kind: 'global' })
				},
				defaultGeometry: registry.defaultGeometry('capability-policy'),
				minGeometry: registry.minGeometry('capability-policy'),
				instanceKey: singletonInstanceKey(
					CAPABILITY_POLICY_PANEL_KIND,
					entryCheck.value,
					GLOBAL
				)
			};
		})()
	};
	window.__maiPolicyApi = windowApi;
</script>

<section data-testid="host-g" aria-label="Host G">
	<ComponentHost label="Host G">
		{#if showG}
			<PanelHost {registry} descriptor={descriptor('${WINDOW_G}', GLOBAL, 1)} {services} />
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-g2" aria-label="Host G2">
	<ComponentHost label="Host G2">
		{#if showG2}
			<PanelHost {registry} descriptor={descriptor('${WINDOW_G2}', GLOBAL, 2)} {services} />
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-sibling" aria-label="Host sibling">
	<ComponentHost label="Host sibling">
		<PanelHost {registry} descriptor={descriptorSibling} />
	</ComponentHost>
</section>
<section data-testid="host-bare" aria-label="Host bare">
	<ComponentHost label="Host bare">
		<PanelHost {registry} descriptor={descriptor('${WINDOW_BARE}', GLOBAL, 5)} />
	</ComponentHost>
</section>
<section data-testid="host-sv" aria-label="Host server-scoped">
	<ComponentHost label="Host server-scoped">
		<PanelHost
			{registry}
			descriptor={descriptor('${WINDOW_SV}', { kind: 'server', server_id: SERVER_A }, 6)}
			{services}
		/>
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
		name: 'mai-policy-harness',
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
			outDir: 'node_modules/.mai-policy-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'policy-[hash].js',
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
<title>MAI capability-policy fixture</title>
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

/** Both global hosts finished their mount read. */
async function waitBothLoaded(page: Page): Promise<void> {
	await expect(page.getByTestId('host-g').getByTestId('policy-revision')).toContainText(
		'Policy revision 1'
	);
	await expect(page.getByTestId('host-g2').getByTestId('policy-revision')).toContainText(
		'Policy revision 1'
	);
}

test('renders distinct enabled-tools and ask control groups; no secret material', async ({
	page
}) => {
	const errors: Error[] = [];
	page.on('pageerror', (error) => errors.push(error));
	await page.goto(FIXTURE_URL);
	const g = page.getByTestId('host-g');
	await waitBothLoaded(page);

	// Acceptance 1: two DISTINCT visible control groups — standing grants and
	// the ask path — plus the deny list with its precedence note.
	await expect(g.getByTestId('grants-section')).toBeVisible();
	await expect(g.getByTestId('deny-section')).toBeVisible();
	await expect(g.getByTestId('approvals-section')).toBeVisible();
	await expect(g.getByTestId('mode-note')).toContainText('Ask/delegated');
	await expect(g.getByTestId('mode-note')).toContainText('never approves an operation');

	// Enabled tools: one row per granted capability, servers as opaque UUIDs.
	const rows = g.getByTestId('grant-row');
	await expect(rows).toHaveCount(3);
	const pkgRow = rows.filter({ hasText: 'pkg.install' });
	await expect(pkgRow.getByTestId('grant-server').locator('code')).toHaveText([
		SERVER_A,
		SERVER_B
	]);

	// Deny precedence is visible, never hidden: shell.raw is granted AND denied.
	const shellRow = rows.filter({ hasText: 'shell.raw' });
	await expect(shellRow.getByTestId('grant-denied-badge')).toHaveText(
		'denied by policy (takes precedence)'
	);
	await expect(g.getByTestId('deny-row')).toHaveCount(1);
	await expect(g.getByTestId('deny-capability')).toHaveText('shell.raw');

	// The ask group is the approvals section with its lookup control.
	await expect(g.getByLabel('Approval id')).toBeVisible();
	await expect(g.getByTestId('approval-load')).toBeVisible();

	// No secret material: the policy surface has no password input at all.
	const content = await page.content();
	expect(content).not.toContain('type="password"');
	await expect(page.locator('input[type="password"]')).toHaveCount(0);

	// Acceptance 3: the component arrived through the entry's real lazy loader.
	expect(servedChunks.length).toBeGreaterThan(0);

	// Acceptance 4: the sibling panel sits beside the policy panel and its
	// interactions drive zero domain traffic.
	const sibling = page.getByTestId('host-sibling');
	await expect(sibling.getByTestId('sibling-panel')).toBeVisible();
	await sibling.getByTestId('sibling-interactions').click();
	await expect(sibling.getByTestId('sibling-interactions')).toHaveText('Sibling interactions: 1');
	expect(await page.evaluate(() => window.__maiPolicyApi!.calls.length)).toBe(2);

	expect(errors).toEqual([]);
});

test('grant revocation commits a versioned edit and survives a remount', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	const g = page.getByTestId('host-g');
	await waitBothLoaded(page);

	await g
		.getByRole('button', { name: `Revoke server ${SERVER_A} from pkg.install` })
		.click();
	await expect(g.getByTestId('policy-saved-notice')).toContainText(
		`Revoked pkg.install on ${SERVER_A}`
	);

	// The write carried the observed revision and the FULL next document
	// (deny list untouched, one server removed from the one grant).
	const [call] = await page.evaluate(() =>
		window.__maiPolicyApi!.calls.filter((entry) => entry.op === 'editGrants')
	);
	expect(call.payload).toEqual({
		expected_revision: 1,
		denied_capabilities: ['shell.raw'],
		grants: [
			{ capability_id: 'pkg.install', server_ids: [SERVER_B] },
			{ capability_id: 'docs.reindex', server_ids: [SERVER_B] },
			{ capability_id: 'shell.raw', server_ids: [SERVER_A] }
		]
	});

	// The silent re-read shows the committed revision; remount re-reads it.
	await expect(g.getByTestId('policy-revision')).toContainText('Policy revision 2');
	await page.evaluate(() => window.__maiPolicyApi!.toggleHost('G'));
	await expect(g.getByTestId('policy-root')).toHaveCount(0);
	await page.evaluate(() => window.__maiPolicyApi!.toggleHost('G'));
	await expect(g.getByTestId('policy-revision')).toContainText('Policy revision 2');
	const remountedRows = g.getByTestId('grant-row').filter({ hasText: 'pkg.install' });
	await expect(remountedRows.getByTestId('grant-server').locator('code')).toHaveText([SERVER_B]);

	const stored = await page.evaluate(() => window.__maiPolicyApi!.config!);
	expect(stored.revision).toBe(2);
});

test('stale second editor displays the conflict, never overwrites; reload recovers', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const g = page.getByTestId('host-g');
	const g2 = page.getByTestId('host-g2');
	await waitBothLoaded(page);

	// First editor commits a revocation (observed revision 1 → committed 2).
	await g2.getByRole('button', { name: `Revoke server ${SERVER_B} from docs.reindex` }).click();
	await expect(g2.getByTestId('policy-saved-notice')).toContainText(
		`Revoked docs.reindex on ${SERVER_B}`
	);

	// Second editor (still observing revision 1) revokes ⇒ 409 conflict.
	await g.getByRole('button', { name: `Revoke server ${SERVER_A} from shell.raw` }).click();
	const banner = g.getByTestId('policy-conflict-banner');
	await expect(banner).toBeVisible();
	await expect(banner).toContainText('nothing was written');

	// Nothing was overwritten: the stored document is exactly editor 2's.
	const stored = await page.evaluate(() => window.__maiPolicyApi!.config!);
	expect(stored.revision).toBe(2);
	expect(stored.grants).toEqual([
		{ capability_id: 'pkg.install', server_ids: [SERVER_A, SERVER_B] },
		{ capability_id: 'shell.raw', server_ids: [SERVER_A] }
	]);

	// Edits stay blocked until the explicit Reload (no silent overwrite).
	await expect(
		g.getByRole('button', { name: `Revoke server ${SERVER_A} from shell.raw` })
	).toBeDisabled();

	await g.getByTestId('policy-reload').click();
	await expect(banner).toHaveCount(0);
	await expect(g.getByTestId('policy-revision')).toContainText('Policy revision 2');
	// The recovered editor can commit again (revision 2 → 3).
	await g.getByRole('button', { name: `Revoke server ${SERVER_A} from shell.raw` }).click();
	await expect(g.getByTestId('policy-saved-notice')).toContainText(
		`Revoked shell.raw on ${SERVER_A}`
	);
	await expect(g.getByTestId('policy-revision')).toContainText('Policy revision 3');
});

test('mount reads only; unmount submits nothing; incomplete mounts fail closed', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	await waitBothLoaded(page);

	// Exactly one read per mounted global host; the fail-closed hosts read 0.
	const bare = page.getByTestId('host-bare');
	await expect(bare.getByTestId('panel-config-error')).toContainText('without its policy service');
	// A server-scoped open of this global-only panel is rejected at the host
	// (registry scope check) — the panel body never mounts and never reads.
	const sv = page.getByTestId('host-sv');
	await expect(sv.getByTestId('panel-unavailable')).toBeVisible();
	expect(sv.getByTestId('policy-root')).toHaveCount(0);
	expect(await page.evaluate(() => window.__maiPolicyApi!.calls)).toHaveLength(2);

	// Unmount (close) submits nothing; remount issues exactly one fresh read.
	await page.evaluate(() => window.__maiPolicyApi!.toggleHost('G'));
	await expect(page.getByTestId('host-g').getByTestId('policy-root')).toHaveCount(0);
	expect(
		await page.evaluate(
			() => window.__maiPolicyApi!.calls.filter((entry) => entry.op !== 'readGrants').length
		)
	).toBe(0);
	await page.evaluate(() => window.__maiPolicyApi!.toggleHost('G'));
	await expect(page.getByTestId('host-g').getByTestId('policy-revision')).toContainText(
		'Policy revision 1'
	);
	expect(await page.evaluate(() => window.__maiPolicyApi!.calls)).toHaveLength(3);
});

test('failure states: unavailable read with retry; edit failure banner', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	await waitBothLoaded(page);
	const g = page.getByTestId('host-g');

	// Unavailable read after remount ⇒ error state with retry (C08 rule 6).
	await page.evaluate(() =>
		window.__maiPolicyApi!.failNextRead({
			status: 503,
			code: 'unavailable',
			message: 'grant configuration service is unavailable'
		})
	);
	await page.evaluate(() => window.__maiPolicyApi!.toggleHost('G'));
	await page.evaluate(() => window.__maiPolicyApi!.toggleHost('G'));
	const errorState = g.getByTestId('policy-error');
	await expect(errorState).toBeVisible();
	await expect(errorState).toHaveAttribute('data-error-code', 'unavailable');
	await g.getByTestId('policy-retry').click();
	await expect(g.getByTestId('policy-revision')).toContainText('Policy revision 1');

	// A failed edit (session ended) is a displayed banner, never silent.
	await page.evaluate(() =>
		window.__maiPolicyApi!.failNextEdit({
			status: 401,
			code: 'unauthenticated',
			message: 'authentication required'
		})
	);
	await g.getByRole('button', { name: `Revoke server ${SERVER_A} from pkg.install` }).click();
	const editError = g.getByTestId('policy-edit-error');
	await expect(editError).toBeVisible();
	await expect(editError).toHaveAttribute('data-error-code', 'unauthenticated');
	await expect(editError).toContainText('Sign in again');
	// The stored document is untouched by the failed edit.
	expect(await page.evaluate(() => window.__maiPolicyApi!.config!.revision)).toBe(1);
});

test('the uncommitted policy is an honest empty state; first commit starts at revision 1', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	await waitBothLoaded(page);
	const g = page.getByTestId('host-g');

	// A data root without a committed document: the read answers 404 — the
	// panel shows an explicit empty state, never a faked default document.
	await page.evaluate(() => window.__maiPolicyApi!.deleteConfig());
	await page.evaluate(() => window.__maiPolicyApi!.toggleHost('G'));
	await page.evaluate(() => window.__maiPolicyApi!.toggleHost('G'));
	await expect(g.getByTestId('policy-empty')).toBeVisible();
	await expect(g.getByTestId('grants-empty')).toHaveText('No tools are enabled.');
	await expect(g.getByTestId('deny-empty')).toHaveText('No tools are disabled.');

	// The empty state still offers the real controls: a revision-0 commit
	// creates revision 1.
	await g.getByTestId('grant-capability-input').fill('pkg.install');
	await g.getByTestId('grant-server-input').fill(SERVER_A);
	await g.getByTestId('grant-add').click();
	await expect(g.getByTestId('policy-saved-notice')).toContainText(
		`Granted pkg.install on ${SERVER_A}`
	);
	await expect(g.getByTestId('policy-revision')).toContainText('Policy revision 1');

	const [call] = await page.evaluate(() =>
		window.__maiPolicyApi!.calls.filter((entry) => entry.op === 'editGrants')
	);
	expect(call.payload).toEqual({
		expected_revision: 0,
		denied_capabilities: [],
		grants: [{ capability_id: 'pkg.install', server_ids: [SERVER_A] }]
	});
});

test('approval preview shows the exact target and the safe binding', async ({ page }) => {
	await page.goto(FIXTURE_URL);
	const g = page.getByTestId('host-g');
	await waitBothLoaded(page);

	// Malformed lookup is caught client-side without a request.
	await g.getByTestId('approval-id-input').fill('not-a-uuid');
	await g.getByTestId('approval-load').click();
	await expect(g.getByText('An approval id is an opaque UUID.')).toBeVisible();
	expect(
		await page.evaluate(
			() => window.__maiPolicyApi!.calls.filter((entry) => entry.op === 'readApproval').length
		)
	).toBe(0);

	await g.getByTestId('approval-id-input').fill(APPROVAL_PENDING);
	await g.getByTestId('approval-load').click();
	const preview = g.getByTestId('approval-preview');
	await expect(preview).toBeVisible();

	// Acceptance 1: the EXACT target (capability x server) is the headline.
	await expect(preview.getByTestId('approval-target')).toContainText('pkg.install');
	await expect(preview.getByTestId('approval-target')).toContainText(SERVER_A);
	// The safe preview: binding digest (never payload bytes), revision, requester.
	await expect(preview.getByTestId('approval-payload-hash')).toHaveText('cd'.repeat(32));
	await expect(preview.getByTestId('approval-payload-hash')).not.toContainText('bytes');
	await expect(preview.getByTestId('approval-policy-revision')).toHaveText('1');
	await expect(preview.getByTestId('approval-status')).toHaveText('pending');
	await expect(preview.getByTestId('approval-created-by')).toHaveText(SESSION_PRINCIPAL);
	await expect(preview.getByTestId('approval-expires-at')).toHaveText('2026-10-14T09:05:00+00:00');
	await expect(preview.getByTestId('approval-decide')).toBeVisible();

	// Unknown id ⇒ visible not-found state with retry.
	await g.getByTestId('approval-id-input').fill('00000000-0000-4000-8000-000000000000');
	await g.getByTestId('approval-load').click();
	const notFound = g.getByTestId('approval-error');
	await expect(notFound).toBeVisible();
	await expect(notFound).toHaveAttribute('data-error-code', 'not_found');
	await expect(g.getByTestId('approval-retry')).toBeVisible();

	// No secret material anywhere in the rendered document.
	expect(await page.locator('input[type="password"]').count()).toBe(0);
});

test('approve presents the binding under the observed revision; receipt only from the response', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const g = page.getByTestId('host-g');
	await waitBothLoaded(page);

	await g.getByTestId('approval-id-input').fill(APPROVAL_PENDING);
	await g.getByTestId('approval-load').click();
	const preview = g.getByTestId('approval-preview');
	await expect(preview.getByTestId('approval-status')).toHaveText('pending');

	await preview.getByTestId('approval-decide').click();

	// The decision POST presented the previewed operation binding under the
	// policy revision the panel observes (C03 rule 4 presenter contract).
	const [call] = await page.evaluate(() =>
		window.__maiPolicyApi!.calls.filter((entry) => entry.op === 'decideApproval')
	);
	expect(call.approvalId).toBe(APPROVAL_PENDING);
	expect(call.payload).toEqual({
		capability_id: 'pkg.install',
		server_id: SERVER_A,
		payload_hash: 'cd'.repeat(32),
		policy_revision: 1
	});

	// The consumed receipt comes from the response — nothing was optimistic.
	await expect(preview.getByTestId('approval-status')).toHaveText('consumed');
	await expect(preview.getByTestId('approval-consumed-by')).toHaveText(OTHER_PRINCIPAL);
	await expect(preview.getByTestId('approval-consumed-at')).toHaveText('2026-10-14T09:01:00+00:00');
	await expect(preview.getByTestId('approval-decide')).toHaveCount(0);

	const stored = await page.evaluate(
		(id) => window.__maiPolicyApi!.approvals[id],
		APPROVAL_PENDING
	);
	expect(stored.status).toBe('consumed');
	expect(stored.consumed_by).toBe(OTHER_PRINCIPAL);
});

test('replay after another session consumed the decision: denial displayed, approval unchanged', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const g = page.getByTestId('host-g');
	await waitBothLoaded(page);

	await g.getByTestId('approval-id-input').fill(APPROVAL_RACE);
	await g.getByTestId('approval-load').click();
	const preview = g.getByTestId('approval-preview');
	await expect(preview.getByTestId('approval-status')).toHaveText('pending');

	// The one-use UPDATE is won by the other admin session before our decide.
	await page.evaluate((id) => window.__maiPolicyApi!.consumeExternally(id), APPROVAL_RACE);
	await preview.getByTestId('approval-decide').click();

	const denial = g.getByTestId('approval-denial');
	await expect(denial).toBeVisible();
	await expect(denial).toHaveAttribute('data-error-code', 'already_consumed');
	await expect(denial).toContainText('one approval authorizes exactly one operation');
	// No optimistic executed-success: the panel still shows the persisted state.
	await expect(preview.getByTestId('approval-status')).toHaveText('pending');
	await expect(preview.getByTestId('approval-decide')).toBeVisible();

	// Re-load shows the honest persisted receipt.
	await g.getByTestId('approval-load').click();
	await expect(preview.getByTestId('approval-status')).toHaveText('consumed');
	await expect(preview.getByTestId('approval-consumed-by')).toHaveText(OTHER_PRINCIPAL);

	// A transport/session failure on decide is a banner, not a denial fake.
	await g.getByTestId('approval-id-input').fill(APPROVAL_PENDING);
	await g.getByTestId('approval-load').click();
	await page.evaluate(() =>
		window.__maiPolicyApi!.failNextDecide({
			status: 401,
			code: 'unauthenticated',
			message: 'authentication required'
		})
	);
	await preview.getByTestId('approval-decide').click();
	const problem = g.getByTestId('approval-problem');
	await expect(problem).toBeVisible();
	await expect(problem).toHaveAttribute('data-error-code', 'unauthenticated');
	await expect(problem).toContainText('Sign in again');
	await expect(preview.getByTestId('approval-status')).toHaveText('pending');
});

test('revoked policy: the stale approval decision is denied and displayed, row untouched', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	const g = page.getByTestId('host-g');
	await waitBothLoaded(page);

	// The pending operation asked under policy revision 1.
	await g.getByTestId('approval-id-input').fill(APPROVAL_PENDING);
	await g.getByTestId('approval-load').click();
	const preview = g.getByTestId('approval-preview');
	await expect(preview.getByTestId('approval-policy-revision')).toHaveText('1');

	// The admin revokes the grant first (revision 1 → 2).
	await g.getByRole('button', { name: `Revoke server ${SERVER_A} from pkg.install` }).click();
	await expect(g.getByTestId('policy-saved-notice')).toContainText('Revoked pkg.install');

	// Approving the stale approval now ⇒ revision_mismatch, DISPLAYED.
	await preview.getByTestId('approval-decide').click();
	const denial = g.getByTestId('approval-denial');
	await expect(denial).toBeVisible();
	await expect(denial).toHaveAttribute('data-error-code', 'revision_mismatch');
	await expect(denial).toContainText('a new approval is required');

	// No executed-success: the approval stays pending and the row is untouched.
	await expect(preview.getByTestId('approval-status')).toHaveText('pending');
	const stored = await page.evaluate(
		(id) => window.__maiPolicyApi!.approvals[id],
		APPROVAL_PENDING
	);
	expect(stored.status).toBe('pending');
	expect(stored.consumed_by).toBe(null);
});

test('feature entry validates in the real registry (scope policy, singleton, geometry)', async ({
	page
}) => {
	await page.goto(FIXTURE_URL);
	await waitBothLoaded(page);

	const registryView = await page.evaluate(() => window.__maiPolicyApi!.registry);
	expect(registryView.kinds).toEqual(['capability-policy', 'fixture-notes']);
	// Global scope is the declared (and only) scope type.
	expect(registryView.scopeChecks.global).toBe('ok');
	expect(registryView.scopeChecks.serverScope).toBe('scope_kind_not_allowed');
	expect(registryView.scopeChecks.unknownKind).toBe('unknown_panel_kind');
	// Declared geometry policies.
	expect(registryView.defaultGeometry).toEqual({
		x: 120,
		y: 88,
		width: 640,
		height: 480
	});
	expect(registryView.minGeometry).toEqual({
		x: 0,
		y: 0,
		width: 420,
		height: 320
	});
	// Singleton per global scope (per-scope dedup key of the state layer).
	expect(registryView.instanceKey).toBe('capability-policy::global');
});
