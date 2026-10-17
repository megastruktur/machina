import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-08-web-lifecycle — real-browser proofs for the application lifecycle
 * surface (`ApplicationActions.svelte`).
 *
 * The harness compiles the REAL reusable component (`ApplicationActions.svelte`)
 * and the shared UI primitives into a throwaway fixture app served from virtual
 * modules — nothing hits disk and no real backend exists. The fixture API
 * mirrors the reviewed producers 1:1 (the `08-app-api` applications surface:
 * the pure plan preview + the three durable `app.*` intent submits, the
 * reviewed run-detail route of `05-jobs-api` and the reviewed approval view of
 * `03-approval-api`); recorded calls and pre-armed responses are the
 * test-observed boundary (the reviewed `01-web` component-browser fixture
 * convention, not an imaginary backend).
 *
 * Covered acceptance surfaces: the bound plan preview with the ACTUAL intended
 * resources and data impact (C09 rule 3); the approval-gated submit with the
 * run replayed from the facade — `waiting_approval` shows the payload binding,
 * a consumed decision shows the binding id (C03 rule 4); an unhealthy update
 * and an uncertain/rolled-back outcome NEVER read as success (C09 rule 4); the
 * removal data decision is EXPLICIT with no default and stays two-tier — a
 * recorded delete intent says the data survived (C09 rule 5); live job linkage
 * through the polled run view + the `openRun` seam; record-document linkage
 * through the `openDocument` seam; identity guards on every response, inert
 * hostile text, disposed-host late responses painting nowhere, fail-closed
 * bags/links, disjoint instances (C01 rule 2, C08 rule 6, C11).
 */

const FIXTURE_URL = '/mai-appactions-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-appactions-app.svelte';
const ENTRY_ID = 'virtual:mai-appactions-entry.js';

const COMPONENT = `${FRONTEND_ROOT}src/lib/components/ApplicationActions.svelte`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const BUTTON = `${FRONTEND_ROOT}src/lib/ui/Button.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const APP_A = '11111111-1111-4222-8333-444455556666';
const APP_B = '33333333-3333-4222-8333-444455556666';
const RUN_A = '44444444-4444-4222-8333-444455556666';
const APPROVAL_ID = '55555555-5555-4222-8333-444455556666';
const RECORD_DOC_ID = '66666666-6666-4222-8333-444455556666';
const PENDING_DOC_ID = '77777777-7777-4222-8333-444455556666';

/** Untruncated runtime-failure receipt: a page error is a harness defect. */
const pageErrors: string[] = [];

type Fail = { status: number; code: string; message: string };

interface LifecycleWindowApi {
	calls: Array<{ op: string; payload?: unknown; args?: unknown[] }>;
	counts: { readRun: number };
	arm(name: 'plan' | 'submit' | 'read' | 'approval', failure: Fail | null): void;
	armDeferredPlan(): void;
	releasePlan(view: unknown): void;
	armDeferredRead(): void;
	releaseRun(detail: unknown): void;
	setRunDetail(detail: unknown): void;
	setPlanOverride(view: unknown | null): void;
	toggleHostA(): void;
}

declare global {
	interface Window {
		__maiLifecycleApi?: LifecycleWindowApi;
		/** Hostile-content canary: set ONLY if inert rendering ever fails. */
		__maiXssFired?: boolean;
	}
}

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; isEntry: boolean; fileName: string; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

// ---------------------------------------------------------------------------
// Producer-authored fixtures (the real catalogue recipe documents, verbatim
// from catalogue/example-stateful.yaml — the 08-fixture-recipes gate fixture)
// ---------------------------------------------------------------------------

const RECIPE_V1 = `schema_version: 1
name: example-stateful
supported_environment:
  os:
    - linux
  arch:
    - aarch64
    - x86_64
prerequisites:
  - docker
required_grants:
  - docker.compose
deploy:
  image: docker.io/filebrowser/filebrowser@sha256:a4da9ca8364b0a1fc5dd36f7add92582bf673c0ae0bda8dd9bd13062c41d1715
  ports:
    - host: 18081
      container: 80
      protocol: tcp
  volumes:
    - name: srv-files
      container_path: /srv
health:
  kind: http
  port: 80
  path: /health
  expected_status: 200
  interval_seconds: 10
  timeout_seconds: 5
  retries: 3
  start_period_seconds: 10
update:
  migrations: none
removal:
  data_policy: explicit_choice
recovery:
  image_rollback: true
backup:
  required: true
`;

const RECIPE_V2 = RECIPE_V1.replace(
	'a4da9ca8364b0a1fc5dd36f7add92582bf673c0ae0bda8dd9bd13062c41d1715',
	'593478e3c24c5ea9f5d7478dc549965b7bc7030707291006ce8d0b6162d3454b'
);

const IMAGE_V1 =
	'docker.io/filebrowser/filebrowser@sha256:a4da9ca8364b0a1fc5dd36f7add92582bf673c0ae0bda8dd9bd13062c41d1715';

/** Deterministic fixture hashes (opaque display material only). */
const H = {
	recipe: 'a'.repeat(64),
	compose: 'b'.repeat(64),
	plan: 'c'.repeat(64),
	payload: 'd'.repeat(64),
	approval: 'e'.repeat(64)
};

// ---------------------------------------------------------------------------
// The fixture app (compiled once per worker; mounted fresh per test)
// ---------------------------------------------------------------------------

const APP_SOURCE = `
<script>
	import ComponentHost from '${HOST}';
	import ApplicationActions from '${COMPONENT}';
	import { ApiError } from '${CLIENT}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const APP_A = '${APP_A}';
	const APP_B = '${APP_B}';
	const RUN_A = '${RUN_A}';
	const APPROVAL_ID = '${APPROVAL_ID}';
	const RECORD_DOC_ID = '${RECORD_DOC_ID}';
	const PENDING_DOC_ID = '${PENDING_DOC_ID}';

	// --- recorded calls + armed behavior -------------------------------------
	// The arms table is the corrected fail-once pattern: throwArm clears the
	// slot AT THE CALL SITE, so an armed failure is consumed exactly once and
	// never decays into fail-always (the apps-spec M1a lesson, re-applied).

	const calls = [];
	const counts = { readRun: 0 };
	const arms = { plan: null, submit: null, read: null, approval: null };

	function record(op, payload, args) {
		calls.push({ op: op, payload: payload, args: args });
	}

	function arm(name, failure) {
		arms[name] = failure;
	}

	function throwArm(name) {
		if (arms[name]) {
			const failure = arms[name];
			arms[name] = null;
			throw new ApiError(failure.status, failure.code, failure.message);
		}
	}

	function clone(value) {
		return value === null || value === undefined ? value : JSON.parse(JSON.stringify(value));
	}

	// Gate lifecycle: arm* parks the NEXT call; the call transfers the gate
	// into the pending* slot where release* finds it — the gate must survive
	// consumption, or the released payload never reaches the component and
	// the late-response assertions turn vacuously green (the apps-spec it2f
	// stranded-gate lesson, re-applied in both flavors).
	let deferredPlan = null;
	let pendingPlan = null;
	let deferredRead = null;
	let pendingRead = null;
	let planOverride = null;
	let runDetail = null;
	let dedupNext = false;

	async function plan(serverId, body) {
		record('plan', clone(body), [serverId]);
		throwArm('plan');
		if (deferredPlan) {
			pendingPlan = deferredPlan;
			const view = await pendingPlan.promise;
			return clone(view);
		}
		return clone(planOverride ?? makePlan(serverId, body));
	}

	function releasePlan(view) {
		const entry = pendingPlan;
		pendingPlan = null;
		deferredPlan = null;
		if (entry) entry.resolve(clone(view));
	}

	async function submit(serverId, applicationId, body) {
		record('submit', clone(body), [serverId, applicationId]);
		throwArm('submit');
		const state = runDetail ? runDetail.state : 'queued';
		return clone({
			run: runView('${RUN_A}', serverId, state),
			deduplicated: dedupNext
		});
	}

	async function readRun(runId) {
		record('readRun', null, [runId]);
		counts.readRun += 1;
		throwArm('read');
		if (deferredRead) {
			pendingRead = deferredRead;
			const detail = await pendingRead.promise;
			return clone(detail);
		}
		if (!runDetail) throw new ApiError(404, 'not_found', 'no such run row');
		return clone(runDetail);
	}

	function releaseRun(detail) {
		const entry = pendingRead;
		pendingRead = null;
		deferredRead = null;
		if (entry) entry.resolve(clone(detail));
	}

	async function readApproval(approvalId) {
		record('readApproval', null, [approvalId]);
		throwArm('approval');
		return clone({
			approval_id: approvalId,
			capability_id: 'app.install',
			server_id: SERVER_A,
			payload_hash: '${H.payload}',
			policy_revision: 3,
			status: 'consumed',
			created_by: 'admin',
			created_at: '2026-10-17T09:00:00Z',
			expires_at: '2026-10-17T10:00:00Z',
			consumed_by: 'worker',
			consumed_at: '2026-10-17T09:05:00Z'
		});
	}

	function openRun(runId) {
		record('openRun', null, [runId]);
	}

	function openDocument(docId) {
		record('openDocument', null, [docId]);
	}

	function openApproval(approvalId) {
		record('openApproval', null, [approvalId]);
	}

	// --- producer-shaped fixture data (mirrors the reviewed views verbatim) --

	function runView(runId, serverId, state) {
		return {
			run_id: runId,
			server_id: serverId,
			parent_run_id: null,
			depth: 0,
			profile_revision: 1,
			prompt_revision: 1,
			model_profile: 'primary',
			requested_grants: ['docker.compose'],
			state: state,
			cancel_requested_at: null,
			submitted_at: '2026-10-17T09:00:00Z',
			updated_at: '2026-10-17T09:00:05Z'
		};
	}

	function step(seq, state, capabilityId, receipt) {
		return { seq: seq, capability_id: capabilityId, state: state, receipt: receipt };
	}

	function makePlan(serverId, body) {
		return {
			server_id: serverId,
			application_id: body.application_id,
			project_name: 'mai-example-stateful',
			target_os: body.target_os,
			target_arch: body.target_arch,
			image: '${IMAGE_V1}',
			recipe_sha256: '${H.recipe}',
			compose_path: '/var/lib/mai/apps/' + body.application_id + '/compose.yaml',
			compose_sha256: '${H.compose}',
			compose_text: 'services:',
			directories: ['/var/lib/mai/apps/' + body.application_id],
			ports: [{ host: 18081, container: 80, protocol: 'tcp' }],
			volumes: [
				{
					name: 'srv-files',
					container_path: '/srv',
					host_path: '/var/lib/mai/apps/' + body.application_id + '/volumes/srv-files'
				}
			],
			credentials: [],
			credentials_path: null,
			health_probe: {
				kind: 'http',
				port: 80,
				path: '/health',
				expected_status: 200,
				interval_seconds: 10,
				timeout_seconds: 5,
				retries: 3,
				start_period_seconds: 10
			},
			prerequisites: ['docker'],
			required_grants: ['docker.compose'],
			operations: [{ op: 'app.install', subject: 'application ' + body.application_id }],
			limitations: [
				'The executor applies only this policy-bound plan; neighboring projects and volumes are never touched.'
			],
			plan_sha256: '${H.plan}'
		};
	}

	const api = {
		plan: plan,
		install: submit,
		update: submit,
		remove: submit,
		readRun: readRun,
		readApproval: readApproval
	};
	const brokenApi = { plan: plan };
	const links = { openRun: openRun, openDocument: openDocument, openApproval: openApproval };

	const recordDocOk = { status: 'documented', doc_id: '${RECORD_DOC_ID}', reason: null };
	const recordDocPending = { status: 'pending', doc_id: '${PENDING_DOC_ID}', reason: 'unavailable' };

	let showA = $state(true);

	function toggleHostA() {
		showA = !showA;
	}

	window.__maiLifecycleApi = {
		calls,
		counts,
		arm,
		armDeferredPlan() {
			deferredPlan = Promise.withResolvers();
		},
		releasePlan,
		armDeferredRead() {
			deferredRead = Promise.withResolvers();
		},
		releaseRun,
		setRunDetail(detail) {
			const dedup = detail && detail.dedup === true;
			runDetail = clone(detail);
			dedupNext = dedup === true;
		},
		setPlanOverride(view) {
			planOverride = view === null ? null : clone(view);
		},
		toggleHostA
	};
</script>

<section data-testid="host-install" aria-label="Install host">
	<ComponentHost label="Install host">
		{#if showA}
			<ApplicationActions
				serverId={SERVER_A}
				applicationId={APP_A}
				mode="install"
				api={api}
				{links}
				recipe={${JSON.stringify(RECIPE_V1)}}
				targetOs="linux"
				targetArch="x86_64"
				recordDoc={recordDocOk}
			/>
		{/if}
	</ComponentHost>
</section>

<section data-testid="host-update" aria-label="Update host">
	<ComponentHost label="Update host">
		<ApplicationActions
			serverId={SERVER_A}
			applicationId={APP_A}
			mode="update"
			api={api}
			recipe={${JSON.stringify(RECIPE_V2)}}
			targetOs="linux"
			targetArch="x86_64"
			currentRecipe={${JSON.stringify(RECIPE_V1)}}
		/>
	</ComponentHost>
</section>

<section data-testid="host-remove" aria-label="Remove host">
	<ComponentHost label="Remove host">
		<ApplicationActions
			serverId={SERVER_A}
			applicationId={APP_A}
			mode="remove"
			api={api}
			{links}
			recipe={${JSON.stringify(RECIPE_V1)}}
			targetOs="linux"
			targetArch="x86_64"
			recordDoc={recordDocOk}
		/>
	</ComponentHost>
</section>

<section data-testid="host-remove-keep" aria-label="Remove keep host">
	<ComponentHost label="Remove keep host">
		<ApplicationActions
			serverId={SERVER_A}
			applicationId={APP_A}
			mode="remove"
			api={api}
			recipe={${JSON.stringify(RECIPE_V1)}}
			targetOs="linux"
			targetArch="x86_64"
			recordDoc={recordDocPending}
		/>
	</ComponentHost>
</section>

<section data-testid="host-nolinks" aria-label="No-links host">
	<ComponentHost label="No-links host">
		<ApplicationActions
			serverId={SERVER_A}
			applicationId={APP_A}
			mode="install"
			api={api}
			recipe={${JSON.stringify(RECIPE_V1)}}
			targetOs="linux"
			targetArch="x86_64"
			recordDoc={recordDocOk}
		/>
	</ComponentHost>
</section>

<section data-testid="host-noapi" aria-label="No-api host">
	<ComponentHost label="No-api host">
		<ApplicationActions
			serverId={SERVER_A}
			applicationId={APP_A}
			mode="install"
			api={brokenApi}
			recipe={${JSON.stringify(RECIPE_V1)}}
			targetOs="linux"
			targetArch="x86_64"
		/>
	</ComponentHost>
</section>

<section data-testid="host-b" aria-label="Server B host">
	<ComponentHost label="Server B host">
		<ApplicationActions
			serverId={SERVER_B}
			applicationId={APP_B}
			mode="install"
			api={api}
			{links}
			recipe={${JSON.stringify(RECIPE_V1)}}
			targetOs="linux"
			targetArch="aarch64"
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

// ---------------------------------------------------------------------------
// Build the harness once per worker (same shape as the reviewed docs/apps
// fixtures); serves the virtual modules and the built chunks
// ---------------------------------------------------------------------------

/** Serves the virtual modules and the built chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-appactions-harness',
		resolveId(id) {
			// .svelte virtual modules must NOT get the conventional `\0` prefix:
			// vite-plugin-svelte excludes `\0` ids from Svelte compilation
			// (panel-host harness convention). The JS entry keeps the `\0`.
			if (id === APP_ID) return id;
			if (id === ENTRY_ID) return `\0${id}`;
			return undefined;
		},
		load(id) {
			if (id === APP_ID) return sources[APP_ID];
			if (id === `\0${ENTRY_ID}`) return sources[ENTRY_ID];
			return undefined;
		}
	};
}

/** Built chunks; the component itself is inlined into the entry chunk here. */
let chunks: Array<{ fileName: string; code: string }> = [];
let harnessJs = '';
let harnessCss = '';

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
			minify: false,
			outDir: 'node_modules/.mai-appactions-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'appactions-[hash].js',
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
});

test.beforeEach(async ({ page }) => {
	pageErrors.length = 0;
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI application actions fixture</title>
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
	// Surface runtime failures in the receipt: an uncaught page error is a
	// harness/component defect, never a silent pass.
	page.on('pageerror', (error) => {
		pageErrors.push(error.stack ?? error.message);
		console.log(`[pageerror] ${error.stack ?? error.message}`);
	});
	page.on('console', (message) => {
		if (message.type() === 'error') console.log(`[console.error] ${message.text()}`);
	});
	await page.goto(FIXTURE_URL);
});

// ---------------------------------------------------------------------------
// Fixture-API boundary (serialized dispatch; same convention as docs/apps)
// ---------------------------------------------------------------------------

/** Call one fixture-api method inside the page; args must be serializable. */
async function apiCall(page: Page, method: string, args: unknown[] = []): Promise<void> {
	const pair: [string, unknown[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiLifecycleApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		// Dynamic dispatch by serialized name: the fixture's own typed API
		// viewed as a name→method table; the typeof guard still checks the
		// lookup before any call.
		const table = value as unknown as Record<string, (...a: never[]) => void>;
		const fn = table[name];
		if (typeof fn !== 'function') throw new Error(`unknown fixture method ${name}`);
		fn.apply(value, callArgs as never[]);
	}, pair);
}

interface RecordedCall {
	op: string;
	payload?: unknown;
	args?: unknown[];
}

/** Snapshot the fixture's recorded calls and counters. */
async function apiState(page: Page): Promise<{
	calls: RecordedCall[];
	counts: LifecycleWindowApi['counts'];
}> {
	return page.evaluate(() => {
		const value = window.__maiLifecycleApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		return { calls: value.calls, counts: { readRun: value.counts.readRun } };
	});
}

/** Test-only recorded-wire boundary: read the LAST recorded op payload. */
function lastCall(calls: RecordedCall[], op: string): RecordedCall {
	const found = calls.filter((call) => call.op === op);
	if (found.length === 0) throw new Error(`no recorded call for op ${op}`);
	return found[found.length - 1];
}

/**
 * Recorded payload boundary: the fixture serializes payloads through
 * `JSON.parse(JSON.stringify(...))` in the page, so the test side receives
 * plain records; the typeof guard keeps a malformed log loud instead of
 * silently misread.
 */
function asPayload(value: unknown): Record<string, unknown> {
	if (typeof value !== 'object' || value === null) {
		throw new Error('recorded payload is not an object');
	}
	// Serialized-JSON boundary: structurally a plain record by construction.
	const record: Record<string, unknown> = value as Record<string, unknown>;
	return record;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** The install host mounted with its recipe prefilled. */
function installRoot(page: Page) {
	return page.getByTestId('host-install').getByTestId('appactions-root');
}

/** Plan on the install host and wait for the bound plan to render. */
async function previewInstallPlan(page: Page): Promise<void> {
	await installRoot(page).getByTestId('plan-preview').click();
	await expect(page.getByTestId('host-install').getByTestId('plan-view')).toBeVisible();
}

/** Plan + submit on the install host; runDetail must be set BEFORE calling. */
async function submitInstall(page: Page): Promise<void> {
	await previewInstallPlan(page);
	await installRoot(page).getByTestId('submit-action').click();
	await expect(page.getByTestId('host-install').getByTestId('run-view')).toBeVisible();
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test('the bound plan preview shows the actual target, resources and data impact; the outgoing call names the pinned scope', async ({
	page
}) => {
	const root = installRoot(page);
	await previewInstallPlan(page);

	await expect(page.getByTestId('host-install').getByTestId('plan-project')).toHaveText(
		'mai-example-stateful'
	);
	await expect(page.getByTestId('host-install').getByTestId('plan-image')).toContainText(
		'sha256:a4da9ca8364b0a1fc5dd36f7add92582bf673c0ae0bda8dd9bd13062c41d1715'
	);
	await expect(page.getByTestId('host-install').getByTestId('plan-compose-path')).toContainText(
		`/var/lib/mai/apps/${APP_A}/compose.yaml`
	);
	await expect(page.getByTestId('host-install').getByTestId('plan-port')).toHaveText(
		'18081 → 80/tcp'
	);
	await expect(page.getByTestId('host-install').getByTestId('plan-volume')).toContainText(
		`/var/lib/mai/apps/${APP_A}/volumes/srv-files`
	);
	await expect(page.getByTestId('host-install').getByTestId('plan-health')).toContainText(
		'/health'
	);
	await expect(page.getByTestId('host-install').getByTestId('plan-grants')).toContainText(
		'docker.compose'
	);
	await expect(page.getByTestId('host-install').getByTestId('plan-operation')).toContainText(
		'app.install'
	);
	await expect(page.getByTestId('host-install').getByTestId('plan-limitations')).toContainText(
		'neighboring projects and volumes are never touched'
	);

	const { calls } = await apiState(page);
	const planCall = lastCall(calls, 'plan');
	expect(planCall.args).toEqual([SERVER_A]);
	expect(planCall.payload).toEqual({
		application_id: APP_A,
		recipe: RECIPE_V1,
		target_os: 'linux',
		target_arch: 'x86_64'
	});
	expect(pageErrors).toEqual([]);
});

test('the bound recipe is locked against edits while its plan is bound; no submit can diverge from the planned bytes', async ({
	page
}) => {
	const root = installRoot(page);
	await previewInstallPlan(page);
	await expect(root.getByTestId('recipe-locked')).toContainText('bound to the previewed plan');
	// The submit routes rebuild the plan deterministically from the submitted
	// recipe — identical inputs yield the identical plan hash. Letting the
	// operator edit the recipe after the plan was bound would desynchronize
	// the approval binding from the submitted bytes, so the input locks.
	await expect(root.getByTestId('recipe-input')).toBeDisabled();
	const { calls } = await apiState(page);
	expect(calls.some((call) => call.op === 'submit')).toBe(false);
	expect(pageErrors).toEqual([]);
});

test('install submits ONE durable intent; a waiting approval shows the payload binding and never reads as success', async ({
	page
}) => {
	const root = installRoot(page);
	await apiCall(page, 'setRunDetail', [
		{
			...runDetailOf(SERVER_A, RUN_A, 'waiting_approval'),
			steps: [
				stepOf(1, 'pending', 'app.install', {
					v: 1,
					op_id: '9'.repeat(32),
					capability_id: 'app.install',
					allowed: false,
					reason: 'approval_required_but_absent',
					effect: 'not_attempted',
					policy_revision: 3,
					approval_id: null,
					approval_spent: false,
					approval_code: null,
					executor_ok: null,
					payload_hash: H.payload
				})
			]
		}
	]);
	await submitInstall(page);

	await expect(page.getByTestId('host-install').getByTestId('run-state')).toHaveAttribute(
		'data-state',
		'waiting_approval'
	);
	await expect(page.getByTestId('host-install').getByTestId('run-state')).toHaveText(
		'Waiting for approval decision'
	);
	await expect(page.getByTestId('host-install').getByTestId('run-state')).not.toHaveClass(
		/mai-appactions__badge--ok/
	);
	await expect(page.getByTestId('host-install').getByTestId('approval-wait')).toContainText(
		'parked until an approval decision exists'
	);
	await expect(page.getByTestId('host-install').getByTestId('step-state')).toHaveText('Pending');

	const { calls } = await apiState(page);
	const submitCall = lastCall(calls, 'submit');
	expect(submitCall.args).toEqual([SERVER_A, APP_A]);
	const payload = asPayload(submitCall.payload);
	expect(payload.recipe).toBe(RECIPE_V1);
	expect(payload.target_os).toBe('linux');
	expect(payload.target_arch).toBe('x86_64');
	expect(payload.backup_confirmed).toBe(false);
	expect(UUID_RE.test(String(payload.run_id))).toBe(true);
	// No approval id exists yet — nothing may claim a consumed decision.
	expect(calls.some((call) => call.op === 'readApproval')).toBe(false);

	await installRoot(page).getByTestId('run-open').click();
	const after = await apiState(page);
	expect(lastCall(after.calls, 'openRun').args).toEqual([RUN_A]);
	expect(pageErrors).toEqual([]);
});

test('a consumed approval renders the binding and opens it in the policy surface; success is exactly the verified terminal state', async ({
	page
}) => {
	const root = installRoot(page);
	await apiCall(page, 'setRunDetail', [
		{
			...runDetailOf(SERVER_A, RUN_A, 'succeeded'),
			steps: [
				stepOf(1, 'succeeded', 'app.install', {
					v: 1,
					op_id: '9'.repeat(32),
					capability_id: 'app.install',
					allowed: true,
					reason: null,
					effect: 'executed',
					policy_revision: 3,
					approval_id: APPROVAL_ID,
					approval_spent: true,
					approval_code: null,
					executor_ok: true,
					payload_hash: H.payload,
					status: 'succeeded'
				})
			]
		}
	]);
	await submitInstall(page);

	await expect(page.getByTestId('host-install').getByTestId('run-state')).toHaveAttribute(
		'data-state',
		'succeeded'
	);
	await expect(page.getByTestId('host-install').getByTestId('step-state')).toHaveText('Succeeded');
	await expect(page.getByTestId('host-install').getByTestId('approval-id')).toBeVisible();
	await expect(page.getByTestId('host-install').getByTestId('approval-spent')).toHaveText(
		'decision consumed'
	);
	await expect(page.getByTestId('host-install').getByTestId('approval-status')).toHaveText(
		'consumed'
	);
	await expect(page.getByTestId('host-install').getByTestId('approval-expires')).toHaveText(
		'2026-10-17T10:00:00Z'
	);

	await root.getByTestId('approval-open').click();
	const { calls } = await apiState(page);
	expect(lastCall(calls, 'readApproval').args).toEqual([APPROVAL_ID]);
	expect(lastCall(calls, 'openApproval').args).toEqual([APPROVAL_ID]);
	expect(pageErrors).toEqual([]);
});

test('an unhealthy update never reads as success: the rolled-back outcome stays open through reconciliation, then lands as failed', async ({
	page
}) => {
	const root = page.getByTestId('host-update').getByTestId('appactions-root');
	await root.getByTestId('plan-preview').click();
	await expect(page.getByTestId('host-update').getByTestId('plan-view')).toBeVisible();

	await apiCall(page, 'setRunDetail', [
		{
			...runDetailOf(SERVER_A, RUN_A, 'running'),
			steps: [stepOf(1, 'pending', 'app.update', null)]
		}
	]);
	await root.getByTestId('submit-action').click();
	await expect(page.getByTestId('host-update').getByTestId('run-view')).toBeVisible();

	const { calls } = await apiState(page);
	const submitCall = lastCall(calls, 'submit');
	expect(submitCall.args).toEqual([SERVER_A, APP_A]);
	const payload = asPayload(submitCall.payload);
	expect(payload.recipe).toBe(RECIPE_V2);
	// The baseline assertion travels with the submit (C09 rule 4).
	expect(payload.current_recipe).toBe(RECIPE_V1);

	// The health regression + verified rollback arrives through the LIVE poll
	// (no user action): step outcome_unknown with the producer's rolled_back
	// receipt — rendered as exactly that, never as success.
	await apiCall(page, 'setRunDetail', [
		{
			...runDetailOf(SERVER_A, RUN_A, 'reconciling'),
			steps: [
				stepOf(1, 'outcome_unknown', 'app.update', {
					v: 1,
					op_id: '9'.repeat(32),
					capability_id: 'app.update',
					allowed: true,
					reason: 'apply_failed',
					effect: 'executed',
					policy_revision: 3,
					approval_id: APPROVAL_ID,
					approval_spent: true,
					approval_code: null,
					executor_ok: false,
					payload_hash: H.payload,
					status: 'rolled_back'
				})
			]
		}
	]);
	await expect(page.getByTestId('host-update').getByTestId('run-state')).toHaveAttribute(
		'data-state',
		'reconciling',
		{ timeout: 5000 }
	);
	await expect(page.getByTestId('host-update').getByTestId('run-state')).toHaveText(
		'Reconciling an unproven outcome'
	);
	await expect(page.getByTestId('host-update').getByTestId('step-state')).toHaveText(
		'Outcome unknown — reconciliation required'
	);
	await expect(page.getByTestId('host-update').getByTestId('step-state')).not.toHaveClass(
		/mai-appactions__badge--ok/
	);
	await expect(page.getByTestId('host-update').getByTestId('receipt-status')).toContainText(
		'rolled back to the prior version — the update did NOT stick'
	);
	await expect(page.getByTestId('host-update').getByTestId('run-state')).not.toHaveClass(
		/mai-appactions__badge--ok/
	);

	// Reconciliation settles the run as failed — the live view follows and the
	// polling STOPS at the terminal state (nothing new can be learned).
	await apiCall(page, 'setRunDetail', [
		{
			...runDetailOf(SERVER_A, RUN_A, 'failed'),
			steps: [
				stepOf(1, 'failed', 'app.update', {
					v: 1,
					op_id: '9'.repeat(32),
					capability_id: 'app.update',
					allowed: true,
					reason: 'apply_failed',
					effect: 'executed',
					policy_revision: 3,
					approval_id: APPROVAL_ID,
					approval_spent: true,
					approval_code: null,
					executor_ok: false,
					payload_hash: H.payload,
					status: 'failed'
				})
			]
		}
	]);
	await expect(page.getByTestId('host-update').getByTestId('run-state')).toHaveAttribute(
		'data-state',
		'failed',
		{ timeout: 5000 }
	);
	await expect(page.getByTestId('host-update').getByTestId('run-state')).toHaveClass(
		/mai-appactions__badge--danger/
	);
	const settled = await apiState(page);
	const countAtTerminal = settled.counts.readRun;
	await page.waitForTimeout(2000);
	const later = await apiState(page);
	expect(later.counts.readRun).toBe(countAtTerminal);
	expect(pageErrors).toEqual([]);
});

test('removal requires the EXPLICIT data decision; delete is a recorded intent only and keep is retained', async ({
	page
}) => {
	const root = page.getByTestId('host-remove').getByTestId('appactions-root');
	await apiCall(page, 'setRunDetail', [
		{
			...runDetailOf(SERVER_A, RUN_A, 'succeeded'),
			steps: [
				stepOf(1, 'succeeded', 'app.remove', {
					v: 1,
					op_id: '9'.repeat(32),
					capability_id: 'app.remove',
					allowed: true,
					reason: null,
					effect: 'executed',
					policy_revision: 3,
					approval_id: null,
					approval_spent: false,
					approval_code: null,
					executor_ok: true,
					payload_hash: H.payload,
					status: 'succeeded'
				})
			]
		}
	]);
	await root.getByTestId('plan-preview').click();
	await expect(page.getByTestId('host-remove').getByTestId('plan-view')).toBeVisible();

	// No default: an unmade decision cannot be submitted (C09 rule 5).
	await expect(root.getByTestId('submit-action')).toBeDisabled();
	await expect(root.getByTestId('data-intent-required')).toBeVisible();
	await expect(page.getByTestId('host-remove').getByTestId('remove-two-tier-note')).toContainText(
		'It NEVER deletes persistent data'
	);

	await root.getByTestId('data-delete').check();
	await expect(root.getByTestId('data-intent-required')).toHaveCount(0);
	await root.getByTestId('submit-action').click();
	await expect(page.getByTestId('host-remove').getByTestId('run-view')).toBeVisible();

	const { calls } = await apiState(page);
	const submitCall = lastCall(calls, 'submit');
	expect(submitCall.args).toEqual([SERVER_A, APP_A]);
	expect(asPayload(submitCall.payload).data_intent).toBe("delete");

	// The tier-1 success says exactly what it did: containers down, intent
	// recorded, data survived — never "data deleted".
	await expect(page.getByTestId('host-remove').getByTestId('data-intent-recorded')).toContainText(
		'delete intent recorded'
	);
	await expect(page.getByTestId('host-remove').getByTestId('data-intent-recorded')).toContainText(
		'was NOT deleted by this removal'
	);
	await expect(page.getByTestId('host-remove').getByTestId('data-intent-recorded')).toContainText(
		'tier-2 purge'
	);
	expect(pageErrors).toEqual([]);
});

test('the keep decision is explicit and renders the retained-data outcome', async ({
	page
}) => {
	const root = page.getByTestId('host-remove-keep').getByTestId('appactions-root');
	await apiCall(page, 'setRunDetail', [
		{
			...runDetailOf(SERVER_A, RUN_A, 'succeeded'),
			steps: [
				stepOf(1, 'succeeded', 'app.remove', {
					v: 1,
					op_id: '9'.repeat(32),
					capability_id: 'app.remove',
					allowed: true,
					reason: null,
					effect: 'executed',
					policy_revision: 3,
					approval_id: null,
					approval_spent: false,
					approval_code: null,
					executor_ok: true,
					payload_hash: H.payload,
					status: 'succeeded'
				})
			]
		}
	]);
	await root.getByTestId('plan-preview').click();
	await expect(page.getByTestId('host-remove-keep').getByTestId('plan-view')).toBeVisible();
	await root.getByTestId('data-keep').check();
	await root.getByTestId('submit-action').click();
	await expect(page.getByTestId('host-remove-keep').getByTestId('run-view')).toBeVisible();

	const { calls } = await apiState(page);
	expect(asPayload(lastCall(calls, "submit").payload).data_intent).toBe("keep");
	await expect(
		page.getByTestId('host-remove-keep').getByTestId('data-intent-recorded')
	).toContainText('retained');
	// The pending record document is honest, never rolled back.
	await expect(
		page.getByTestId('host-remove-keep').getByTestId('record-doc-pending')
	).toContainText('unavailable');
	expect(pageErrors).toEqual([]);
});

test('a duplicate submit attaches to the existing run and says so; links and docs fail closed without their seam', async ({
	page
}) => {
	const root = page.getByTestId('host-nolinks').getByTestId('appactions-root');
	await apiCall(page, 'setRunDetail', [
		{ ...runDetailOf(SERVER_A, RUN_A, 'queued', { dedup: true }), steps: [] }
	]);
	await root.getByTestId('plan-preview').click();
	await expect(page.getByTestId('host-nolinks').getByTestId('plan-view')).toBeVisible();
	await root.getByTestId('submit-action').click();
	await expect(page.getByTestId('host-nolinks').getByTestId('run-view')).toBeVisible();
	await expect(page.getByTestId('host-nolinks').getByTestId('run-dedup')).toContainText(
		'attached to the existing run'
	);

	// Without the links bag the run/doc identities stay displayed text —
	// never a fake action.
	await expect(page.getByTestId('host-nolinks').getByTestId('run-link-unavailable')).toContainText(
		'not wired'
	);
	await expect(
		page.getByTestId('host-nolinks').getByTestId('record-doc-link-unavailable')
	).toContainText('not wired');

	// With the bag wired, the record document opens through the docs seam.
	await installRoot(page).getByTestId('record-doc-open').click();
	const { calls } = await apiState(page);
	expect(lastCall(calls, 'openDocument').args).toEqual([RECORD_DOC_ID]);
	expect(pageErrors).toEqual([]);
});

test('a failed run read is an honest outage with the durable intent preserved; refresh recovers', async ({
	page
}) => {
	const root = installRoot(page);
	await apiCall(page, 'setRunDetail', [
		{ ...runDetailOf(SERVER_A, RUN_A, 'queued'), steps: [] }
	]);
	await apiCall(page, 'arm', [
		'read',
		{ status: 503, code: 'unavailable', message: 'jobs domain unavailable' }
	]);
	await submitInstall(page);

	await expect(page.getByTestId('host-install').getByTestId('run-error')).toContainText(
		'unavailable'
	);
	await expect(page.getByTestId('host-install').getByTestId('run-error')).toContainText(
		'the intent is durable'
	);

	await apiCall(page, 'setRunDetail', [
		{
			...runDetailOf(SERVER_A, RUN_A, 'running'),
			steps: [stepOf(1, 'running', 'app.install', null)]
		}
	]);
	await installRoot(page).getByTestId('run-refresh').click();
	await expect(page.getByTestId('host-install').getByTestId('run-state')).toHaveAttribute(
		'data-state',
		'running'
	);
	await expect(page.getByTestId('host-install').getByTestId('run-error')).toHaveCount(0);
	expect(pageErrors).toEqual([]);
});

test('a cross-server plan answer cannot paint; a late plan answer to a disposed host paints nowhere', async ({
	page
}) => {
	// Cross-server: the plan answer names Server A while the host pins B —
	// the identity guard refuses it before anything binds.
	const rootB = page.getByTestId('host-b').getByTestId('appactions-root');
	await apiCall(page, 'setPlanOverride', [{ ...planOf(SERVER_A, APP_A), server_id: SERVER_A }]);
	await apiCall(page, 'armDeferredPlan');
	await rootB.getByTestId('plan-preview').click();
	await apiCall(page, 'releasePlan', [planOf(SERVER_A, APP_A)]);
	await expect(rootB.getByTestId('plan-error')).toContainText('different target');
	await expect(page.getByTestId('host-b').getByTestId('plan-view')).toHaveCount(0);

	// Disposed host: the parked answer resolves after the host is gone; it
	// must paint nowhere and throw nothing.
	await apiCall(page, 'armDeferredPlan');
	await installRoot(page).getByTestId('plan-preview').click();
	await apiCall(page, 'toggleHostA');
	await apiCall(page, 'releasePlan', [planOf(SERVER_A, APP_A)]);
	await expect(page.getByTestId('host-install').getByTestId('appactions-root')).toHaveCount(0);
	await apiCall(page, 'toggleHostA');
	const root = installRoot(page);
	await expect(root).toBeVisible();
	// The remounted instance starts clean — no stale plan revived.
	await expect(page.getByTestId('host-install').getByTestId('plan-view')).toHaveCount(0);
	const { calls } = await apiState(page);
	const planCalls = calls.filter((call) => call.op === 'plan');
	for (const call of planCalls) {
		expect(call.args?.[0] === SERVER_A || call.args?.[0] === SERVER_B).toBe(true);
	}
	expect(pageErrors).toEqual([]);
});

test('hostile producer text and a hostile recipe render inert; the fail-closed bag and disjoint instances hold', async ({
	page
}) => {
	// The hostile recipe is typed BEFORE any plan binds: once a plan is bound
	// the input locks, so the operator's text is captured while still editable.
	const hostileRecipe =
		'<script>window.__maiXssFired = true;</script>\n<iframe src="javascript:window.__maiXssFired = true"></iframe>';
	const root = installRoot(page);
	await root.getByTestId('recipe-input').fill(hostileRecipe);

	const hostile = {
		...planOf(SERVER_A, APP_A),
		limitations: [
			'<img src=x onerror="window.__maiXssFired = true"> The executor applies only this policy-bound plan.'
		],
		compose_path: '<script>window.__maiXssFired = true;</script>/srv/app/compose.yaml'
	};
	await apiCall(page, 'setPlanOverride', [hostile]);
	await root.getByTestId('plan-preview').click();
	await expect(page.getByTestId('host-install').getByTestId('plan-view')).toBeVisible();
	await expect(page.getByTestId('host-install').getByTestId('plan-limitations')).toContainText(
		'The executor applies only this policy-bound plan.'
	);
	// The hostile recipe travels verbatim inside the recorded call payload
	// (JSON, never markup) and stays inert in the locked textarea.
	await expect(root.getByTestId('recipe-input')).toHaveValue(hostileRecipe);

	const inert = await page.evaluate(() => {
		const roots = Array.from(document.querySelectorAll('[data-testid="appactions-root"]'));
		return {
			xssFired: window.__maiXssFired === true,
			scripts: roots.reduce(
				(total, root) => total + root.querySelectorAll('script, iframe, img').length,
				0
			)
		};
	});
	expect(inert.xssFired).toBe(false);
	expect(inert.scripts).toBe(0);

	// The incomplete bag renders the honest config error and records nothing.
	await expect(page.getByTestId('host-noapi').getByTestId('appactions-config-error')).toContainText(
		'without its complete lifecycle service'
	);

	// Two instances act on disjoint scopes; every outgoing call carries its
	// own host's pinned identity. (The hostile override is cleared first —
	// its answer names Server A and must never bind on host B either.)
	await apiCall(page, 'setPlanOverride', [null]);
	const rootB = page.getByTestId('host-b').getByTestId('appactions-root');
	await rootB.getByTestId('plan-preview').click();
	await expect(page.getByTestId('host-b').getByTestId('plan-view')).toBeVisible();
	const { calls } = await apiState(page);
	for (const call of calls) {
		if (call.op === 'plan') {
			expect([SERVER_A, SERVER_B]).toContain(call.args?.[0]);
			const payload = asPayload(call.payload);
			if (call.args?.[0] === SERVER_A) expect(payload.application_id).toBe(APP_A);
			if (call.args?.[0] === SERVER_B) expect(payload.application_id).toBe(APP_B);
		}
	}
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// Fixture-shape helpers (test-side mirrors of the producer views)
// ---------------------------------------------------------------------------

function runDetailOf(
	serverId: string,
	runId: string,
	state: string,
	extra: { dedup?: boolean } = {}
) {
	return {
		run_id: runId,
		server_id: serverId,
		parent_run_id: null,
		depth: 0,
		profile_revision: 1,
		prompt_revision: 1,
		model_profile: 'primary',
		requested_grants: ['docker.compose'],
		state,
		cancel_requested_at: null,
		submitted_at: '2026-10-17T09:00:00Z',
		updated_at: '2026-10-17T09:00:05Z',
		steps: [] as unknown[],
		dedup: extra.dedup === true ? true : undefined
	};
}

function stepOf(
	seq: number,
	state: string,
	capabilityId: string,
	receipt: Record<string, unknown> | null
) {
	return { seq, capability_id: capabilityId, state, receipt };
}

function planOf(serverId: string, applicationId: string) {
	return {
		server_id: serverId,
		application_id: applicationId,
		project_name: 'mai-example-stateful',
		target_os: 'linux',
		target_arch: 'x86_64',
		image: IMAGE_V1,
		recipe_sha256: H.recipe,
		compose_path: `/var/lib/mai/apps/${applicationId}/compose.yaml`,
		compose_sha256: H.compose,
		compose_text: 'services:',
		directories: [`/var/lib/mai/apps/${applicationId}`],
		ports: [{ host: 18081, container: 80, protocol: 'tcp' }],
		volumes: [
			{
				name: 'srv-files',
				container_path: '/srv',
				host_path: `/var/lib/mai/apps/${applicationId}/volumes/srv-files`
			}
		],
		credentials: [],
		credentials_path: null,
		health_probe: {
			kind: 'http',
			port: 80,
			path: '/health',
			expected_status: 200,
			interval_seconds: 10,
			timeout_seconds: 5,
			retries: 3,
			start_period_seconds: 10
		},
		prerequisites: ['docker'],
		required_grants: ['docker.compose'],
		operations: [{ op: 'app.install', subject: `application ${applicationId}` }],
		limitations: [
			'The executor applies only this policy-bound plan; neighboring projects and volumes are never touched.'
		],
		plan_sha256: H.plan
	};
}
