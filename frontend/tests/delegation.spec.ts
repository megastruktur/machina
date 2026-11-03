import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-06-web-delegation — real-browser proofs for the delegation tree view
 * (frontend/src/lib/components/DelegationTree.svelte).
 *
 * Focused per the test policy: the reusable tree is exercised in real
 * Chromium, mounted through the reviewed component-host fixture with a
 * fixture in-page API implementing the component's typed `DelegationApi`
 * shape (the interface the conversation panel will satisfy from
 * `createMaiApiClient()` at composition, mirroring the reviewed
 * `mai.api.chat` routes 1:1). The REAL backend boundary — envelope codes,
 * session/origin/CSRF, delegation/budget ledger persistence — is owned by
 * the `06-chat-api`/`06-delegation-budgets` producer suites; the assembled
 * desktop journey (tree inside the real chat panel beside other panels) is
 * owned by `06-verify` at the phase gate.
 *
 * Harness (self-contained, following the jobs/models convention):
 *  1. test.beforeAll runs an IN-MEMORY Vite production build (write: false)
 *     of a virtual app importing the REAL tree component, the REAL
 *     component-host fixture and the REAL client primitive. The bag's
 *     `streamConversationEvents` is the REAL `subscribeEvents` over the
 *     reviewed chat producer URL, answered by Playwright route handlers —
 *     the concrete URL, `Last-Event-ID` cursor carry, frame parsing and
 *     reconnect loop are exercised on the wire, not stubbed.
 *  2. Each test serves the built JS/CSS/HTML through route interception.
 *     The fixture API mirrors the reviewed producer semantics (replayed
 *     durable rows, honest cancel with `outcome: null` until the engine
 *     answers, value-free ledger events on the ROOT run's stream only).
 */

const FIXTURE_URL = '/mai-delegation-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-delegation-app.svelte';
// The rollup input is a plain JS entry module (the .svelte virtual module
// tree-shakes away as an input; the reviewed harness convention).
const ENTRY_ID = 'virtual:mai-delegation-entry.js';

const TREE = `${FRONTEND_ROOT}src/lib/components/DelegationTree.svelte`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const ROOT = 'aa111111-1111-4222-8333-444455556666';
const CHILD_A = 'bb222222-1111-4222-8333-444455556666';
const CHILD_B = 'cc333333-1111-4222-8333-444455556666';
const CHILD_C = 'dd444444-1111-4222-8333-444455556666';
const BARE_ROOT = 'ee555555-1111-4222-8333-444455556666';

/**
 * Secret-SHAPED fixture canary (synthetic, NOT a secret): stands for the
 * conversation/task/context content the wire never exposes. It lives in the
 * fixture store only and must NEVER appear in the DOM (C05 rule 4: events
 * and rows are value-free; the tree renders opener labels, nothing else).
 */
const FIXTURE_CANARY = 'MAI-FIXTURE-CANARY-context-body-NEVER-DISPLAY';

/** Task labels the opener authors from its own submitted ChatSubmitBody. */
const TASK_LABELS: Record<string, string> = {
	[ROOT]: 'Review the fixture deployment end to end.',
	[CHILD_A]: 'child aspect-alpha review',
	[CHILD_B]: 'child aspect-beta review'
};

type Fail = { status: number; code: string; message: string };

type Scenario = 'default' | 'states' | 'empty';

interface DelegationWindowApi {
	calls: Array<{ op: string; runId?: string; payload?: unknown; cursor?: string | null }>;
	db: { conversation: Record<string, unknown> };
	failNextRead(failure: Fail | null): void;
	armCancel(outcome: string | null): void;
	setChildren(children: Array<Record<string, unknown>>): void;
	setRootState(state: string): void;
	toggleMain(on: boolean): void;
}

declare global {
	interface Window {
		__maiDelegationApi?: DelegationWindowApi;
		/** Set by a test init script: the boot scenario. */
		__maiBootScenario?: Scenario;
	}
}

type RecordedCall = DelegationWindowApi['calls'][number];

/**
 * Test seam: recorded fixture calls of one op (serialized VALUES only —
 * fixture methods never cross the evaluate boundary).
 */
async function callsOf(page: Page, op: string): Promise<RecordedCall[]> {
	return page.evaluate((operation) => {
		const api = window.__maiDelegationApi;
		return api ? api.calls.filter((call) => call.op === operation) : [];
	}, op);
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
 * The fixture app. MAIN mounts the tree with the full bag; BARE mounts with
 * NO services (fail-closed). MAIN is toggleable for the unmount-release
 * proof. Virtual .svelte modules compile in RUNES mode in this in-memory
 * build (observed convention), so the app uses `$state` only.
 */
const APP_SOURCE = `
<script>
	import DelegationTree from '${TREE}';
	import ComponentHost from '${HOST}';
	import { ApiError, subscribeEvents } from '${CLIENT}';

	const SERVER = '${SERVER}';
	const ROOT = '${ROOT}';
	const BARE_ROOT = '${BARE_ROOT}';
	const CANARY = '${FIXTURE_CANARY}';

	function clone(value) {
		return structuredClone(JSON.parse(JSON.stringify(value)));
	}

	function runRow(runId, state, grants, overrides) {
		return Object.assign(
			{
				run_id: runId,
				server_id: SERVER,
				parent_run_id: null,
				depth: 0,
				profile_revision: 3,
				prompt_revision: 1,
				model_profile: 'fast',
				requested_grants: grants,
				state,
				cancel_requested_at: null,
				submitted_at: '2026-10-16T09:00:00Z',
				updated_at: '2026-10-16T09:01:00Z'
			},
			overrides ?? {}
		);
	}

	function childRow(runId, state, grants) {
		return runRow(runId, state, grants, {
			parent_run_id: ROOT,
			depth: 1
		});
	}

	// --- fixture backend: replayed durable rows (the facade never guesses) ----

	const scenario = window.__maiBootScenario ?? 'default';
	let startChildren;
	if (scenario === 'empty') {
		startChildren = [];
	} else if (scenario === 'states') {
		startChildren = [
			childRow('${CHILD_A}', 'failed', ['fixture.read']),
			childRow('${CHILD_B}', 'cancelled', ['fixture.read']),
			// Grant-denied child: the request clamped to an EMPTY effective set.
			childRow('${CHILD_C}', 'succeeded', [])
		];
	} else {
		startChildren = [
			childRow('${CHILD_A}', 'succeeded', ['fixture.read']),
			childRow('${CHILD_B}', 'failed', ['fixture.read'])
		];
	}
	const db = {
		conversation: Object.assign(runRow(ROOT, 'running', ['fixture.read'], { children: startChildren }), {
			// Wire-invisible context body the tree must never render.
			message: CANARY
		})
	};

	const calls = [];
	let failNextRead = null;
	let cancelOutcome = null;

	const api = {
		async readConversation(runId) {
			calls.push({ op: 'readConversation', runId });
			if (failNextRead) {
				const failure = failNextRead;
				failNextRead = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			if (runId !== db.conversation.run_id) {
				throw new ApiError(404, 'not_found', 'no conversation stored under this id');
			}
			return clone(db.conversation);
		},
		async cancelConversation(runId, payload) {
			calls.push({ op: 'cancelConversation', runId, payload: JSON.parse(JSON.stringify(payload)) });
			if (runId !== db.conversation.run_id) {
				throw new ApiError(404, 'not_found', 'no conversation stored under this id');
			}
			// Producer strictness: the explicit tree-cancel body is REQUIRED.
			if (payload.cancel_children !== true) {
				throw new ApiError(422, 'validation_failed', 'cancel_children must be true');
			}
			const outcome = cancelOutcome;
			cancelOutcome = null;
			if (db.conversation.cancel_requested_at === null) {
				db.conversation.cancel_requested_at = '2026-10-16T09:05:00Z';
			}
			if (outcome === 'cancelled') {
				db.conversation.state = 'cancelled';
				db.conversation.updated_at = '2026-10-16T09:06:00Z';
			}
			return {
				run_id: runId,
				state: db.conversation.state,
				outcome,
				requested_at: '2026-10-16T09:05:00Z',
				outcome_at: outcome === 'cancelled' ? '2026-10-16T09:06:00Z' : null,
				requested_now: true,
				unresolved_seqs: []
			};
		},
		async streamConversationEvents(runId, options) {
			calls.push({ op: 'streamConversationEvents', runId, cursor: options.cursor ?? null });
			// The reviewed producer route (mai.api.chat stream_conversation_events);
			// the real URL binding belongs to the conversation entry at composition.
			return subscribeEvents(
				'/api/v1/chat/runs/' + encodeURIComponent(runId) + '/events',
				options
			);
		}
	};

	let mainMounted = $state(true);
	const taskLabels = ${JSON.stringify(TASK_LABELS)};

	window.__maiDelegationApi = {
		calls,
		db,
		failNextRead(failure) {
			failNextRead = failure;
		},
		armCancel(outcome) {
			cancelOutcome = outcome;
		},
		setChildren(children) {
			db.conversation.children = children;
		},
		setRootState(state) {
			db.conversation.state = state;
			db.conversation.updated_at = '2026-10-16T09:07:00Z';
		},
		toggleMain(on) {
			mainMounted = on;
		}
	};
`;

// The app template (JSX-free Svelte markup) is appended below the script so
// the fixture store stays above its consumers.
const APP_TEMPLATE = `
</script>

<div class="fixture">
	{#if mainMounted}
		<ComponentHost label="delegation main">
			<DelegationTree conversation={db.conversation} api={api} taskLabels={taskLabels} />
		</ComponentHost>
	{/if}
	<ComponentHost label="delegation bare">
		<DelegationTree conversation={{ run_id: BARE_ROOT, server_id: SERVER, parent_run_id: null, depth: 0, profile_revision: 1, prompt_revision: 1, model_profile: 'fast', requested_grants: [], state: 'running', cancel_requested_at: null, submitted_at: '2026-10-16T09:00:00Z', updated_at: '2026-10-16T09:00:00Z', children: [] }} api={null} />
	</ComponentHost>
</div>
`;

const APP_SOURCE_FULL = APP_SOURCE + APP_TEMPLATE;

const ENTRY_SOURCE = `
import { mount } from 'svelte';
import App from '${APP_ID}';
import '${TOKENS}';
mount(App, { target: document.getElementById('app') });
`;

// ---------------------------------------------------------------------------
// Event-stream wire control (real fetch, answered by route handlers)
// ---------------------------------------------------------------------------

interface StreamFrame {
	seq: number;
	type: string;
	data?: unknown;
}

interface StreamSession {
	/** Frames to deliver before the stream closes; empty for a bare close. */
	frames?: StreamFrame[];
	/** Reject the connection (API restart / network failure) instead of closing. */
	abort?: boolean;
}

let streamSessions: StreamSession[] = [];
let streamRequests: Array<{ runId: string; cursor: string | null; path: string }> = [];
/** Requests held open because no session was queued yet (the loop pauses). */
let heldStreamRoutes: Route[] = [];

function sseBody(runId: string, frames: StreamFrame[], cursor: string | null): string {
	// Producer replay semantics: only frames strictly after the cursor travel
	// on a resumed subscription (C08 rule 7). The consumer watermark is the
	// second line of defense and is proven separately.
	const after = frames.filter(
		(frame) => cursor === null || frame.seq > Number(cursor)
	);
	return after
		.map(
			(frame) =>
				`id: ${frame.seq}\n` +
				`data: ${JSON.stringify({
					seq: frame.seq,
					run_id: runId,
					ts: '2026-10-16T10:00:00Z',
					type: frame.type,
					v: 1,
					data: frame.data ?? {}
				})}\n\n`
		)
		.join('');
}

function fulfillStream(route: Route, session: StreamSession, cursor: string | null): Promise<void> {
	return route.fulfill({
		status: 200,
		contentType: 'text/event-stream',
		headers: { 'cache-control': 'no-store' },
		body: sseBody(ROOT, session.frames ?? [], cursor)
	});
}

/**
 * One SSE request: recorded (run id, cursor, path), then either answered
 * from the queued session or held open — a held request blocks the view's
 * await, which keeps the reconnect loop deterministic between sessions.
 */
async function handleEventStream(route: Route): Promise<void> {
	const url = new URL(route.request().url());
	const match = url.pathname.match(/^\/api\/v1\/chat\/runs\/([^/]+)\/events$/);
	if (!match) {
		await route.abort();
		return;
	}
	const cursor = route.request().headers()['last-event-id'] ?? null;
	streamRequests.push({ runId: match[1], cursor, path: url.pathname });
	const session = streamSessions.shift();
	if (session) {
		if (session.abort) await route.abort();
		else await fulfillStream(route, session, cursor);
		return;
	}
	heldStreamRoutes.push(route);
}

/** Queue a session and release the oldest held request with it. */
function queueStreamSession(session: StreamSession): void {
	streamSessions.push(session);
	const route = heldStreamRoutes.shift();
	if (!route) return;
	const next = streamSessions.shift();
	if (!next) {
		heldStreamRoutes.unshift(route);
		return;
	}
	const request = streamRequests[streamRequests.length - 1];
	void (next.abort
		? route.abort()
		: fulfillStream(route, next, request?.cursor ?? null)
	).catch(() => {
		// The page may have moved on between queueing and releasing; the
		// assertion on the recorded requests is the source of truth.
	});
}

/** Recorded stream requests, awaited between steps (no fixed sleeps). */
async function waitForStreamRequests(count: number): Promise<void> {
	await expect
		.poll(() => streamRequests.length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(count);
}

// ---------------------------------------------------------------------------

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-delegation-harness',
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

/** Built chunks; the harness is one entry chunk plus one CSS asset. */
let chunks: Array<{ fileName: string; code: string }> = [];

test.beforeAll(async () => {
	const sources: Record<string, string> = {
		[APP_ID]: APP_SOURCE_FULL,
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
			outDir: 'node_modules/.mai-delegation-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'delegation-[hash].js',
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

const BOOT_HTML = `<!doctype html>
<html>
<head><meta charset="utf-8"><title>mai delegation fixture</title></head>
<body>
	<div id="app"></div>
	<script type="module" src="${FIXTURE_URL}demo.js"></script>
</body>
</html>`;

test.beforeEach(async ({ page }) => {
	chunks = [];
	streamSessions = [];
	streamRequests = [];
	heldStreamRoutes = [];

	// Serve the virtual app; every non-fixture request aborts so the tree can
	// never silently depend on some other origin.
	await page.route('**/*', (route) => {
		const { pathname } = new URL(route.request().url());
		if (!pathname.startsWith(FIXTURE_URL)) return route.abort();
		const rest = pathname.slice(FIXTURE_URL.length);
		if (rest === '' || rest.endsWith('/')) {
			return route.fulfill({ contentType: 'text/html; charset=utf-8', body: BOOT_HTML });
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
	await page.route('**/api/v1/chat/runs/*/events', (route) => handleEventStream(route));
	// Surface runtime failures in the receipt: an uncaught page error is a
	// harness/component defect, never a silent pass.
	page.on('pageerror', (error) => {
		throw new Error(`fixture page error: ${error.message}`);
	});
});

async function openFixture(page: Page, scenario: Scenario): Promise<void> {
	await page.addInitScript((value) => {
		window.__maiBootScenario = value;
	}, scenario);
	await page.goto(FIXTURE_URL);
	await expect(page.getByTestId('component-host')).toHaveCount(2);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test('renders the persisted tree: statuses, model, task labels, target, provenance; no secret context', async ({ page }) => {
	await openFixture(page, 'default');
	const main = page.getByTestId('component-host').first();

	// Root node: actual replayed state, model, target, identity, depth, grants.
	const root = main.getByTestId('root-node');
	await expect(root).toBeVisible();
	await expect(main.getByTestId('root-state')).toHaveAttribute('data-state', 'active');
	await expect(main.getByTestId('root-state')).toContainText('Running');
	await expect(main.getByTestId('root-task')).toHaveText(TASK_LABELS[ROOT]);
	await expect(main.getByTestId('root-model')).toHaveText('Model fast');
	await expect(main.getByTestId('root-target')).toHaveText(`Target ${SERVER.slice(0, 8)}`);
	await expect(main.getByTestId('root-id')).toHaveText(`Run ${ROOT.slice(0, 8)}`);
	await expect(main.getByTestId('root-depth')).toHaveText('depth 0');
	await expect(main.getByTestId('root-grants')).toContainText('fixture.read');

	// Child rows: distinct persisted states with full provenance.
	const rows = main.getByTestId('child-row');
	await expect(rows).toHaveCount(2);
	await expect(rows.nth(0)).toHaveAttribute('data-run-id', CHILD_A);
	await expect(rows.nth(0)).toHaveAttribute('data-source', 'row');
	await expect(main.getByTestId('child-state').nth(0)).toHaveAttribute('data-state', 'succeeded');
	await expect(main.getByTestId('child-state').nth(1)).toHaveAttribute('data-state', 'failed');
	await expect(main.getByTestId('child-task').nth(0)).toHaveText(TASK_LABELS[CHILD_A]);
	await expect(main.getByTestId('child-task').nth(1)).toHaveText(TASK_LABELS[CHILD_B]);
	for (let index = 0; index < 2; index += 1) {
		await expect(main.getByTestId('child-model').nth(index)).toHaveText('Model fast');
		await expect(main.getByTestId('child-target').nth(index)).toHaveText(`Target ${SERVER.slice(0, 8)}`);
		await expect(main.getByTestId('child-parent').nth(index)).toHaveText(
			`child of ${ROOT.slice(0, 8)} · depth 1`
		);
	}

	// Budget: no ledger events yet ⇒ the explicit empty state, never zeros.
	await expect(main.getByTestId('budget-none')).toContainText(
		'No budget ledger events recorded for this tree yet'
	);

	// No granted-less child marker in this scenario.
	await expect(main.getByTestId('child-grants-none')).toHaveCount(0);

	// The tree copies no secret context: the canary context body stays in the
	// fixture store and never reaches the DOM.
	const body = await page.locator('body').innerText();
	expect(body).not.toContain(FIXTURE_CANARY);
});

test('discovers planned children from the stream exactly once; rows resolve them; replay never duplicates', async ({ page }) => {
	await openFixture(page, 'empty');
	const main = page.getByTestId('component-host').first();
	await expect(main.getByTestId('children-none')).toBeVisible();

	// Session 1: the parent plans two children (value-free identities).
	queueStreamSession({
		frames: [
			{
				seq: 1,
				type: 'agent.delegation_planned',
				data: { children: [CHILD_A, CHILD_B], depth: 1 }
			}
		]
	});
	await waitForStreamRequests(1);
	const rows = main.getByTestId('child-row');
	await expect(rows).toHaveCount(2);
	await expect(rows.nth(0)).toHaveAttribute('data-source', 'event');
	await expect(main.getByTestId('child-state').nth(0)).toContainText('State not recorded yet');
	await expect(main.getByTestId('child-parent').nth(0)).toHaveText(
		`child of ${ROOT.slice(0, 8)} · depth 1`
	);

	// The closed session triggers the reconnect (cursor '1'); await the
	// request, THEN queue session 2 — it lands on request 2 whether that
	// request is already held or still arriving.
	await waitForStreamRequests(2);
	queueStreamSession({
		frames: [
			{
				seq: 2,
				type: 'agent.children_resolved',
				data: {
					children: [
						{ run_id: CHILD_A, state: 'succeeded' },
						{ run_id: CHILD_B, state: 'failed' }
					]
				}
			},
			// The durable-projection event: run.transition (not the fold-only
			// resolutions) is what justifies the reread that lands the rows.
			{
				seq: 3,
				type: 'run.transition',
				data: { from: 'running', to: 'waiting_children' }
			}
		]
	});
	await expect(main.getByTestId('child-state').nth(0)).toHaveAttribute('data-state', 'succeeded');
	await expect(main.getByTestId('child-state').nth(1)).toHaveAttribute('data-state', 'failed');

	// The durable refresh lands the persisted rows; rows win, node count stays 2.
	await page.evaluate((rows) => {
		window.__maiDelegationApi?.setChildren(rows);
	}, [
		{
			run_id: CHILD_A,
			server_id: SERVER,
			parent_run_id: ROOT,
			depth: 1,
			profile_revision: 3,
			prompt_revision: 1,
			model_profile: 'fast',
			requested_grants: ['fixture.read'],
			state: 'succeeded',
			cancel_requested_at: null,
			submitted_at: '2026-10-16T09:02:00Z',
			updated_at: '2026-10-16T09:03:00Z'
		},
		{
			run_id: CHILD_B,
			server_id: SERVER,
			parent_run_id: ROOT,
			depth: 1,
			profile_revision: 3,
			prompt_revision: 1,
			model_profile: 'fast',
			requested_grants: ['fixture.read'],
			state: 'failed',
			cancel_requested_at: null,
			submitted_at: '2026-10-16T09:02:00Z',
			updated_at: '2026-10-16T09:04:00Z'
		}
	]);
	await expect
		.poll(async () => (await callsOf(page, 'readConversation')).length, { timeout: 10000 })
		.toBeGreaterThanOrEqual(1);
	await expect(rows).toHaveCount(2);
	await expect(rows.nth(0)).toHaveAttribute('data-source', 'row');
	await expect(main.getByTestId('child-model').nth(0)).toHaveText('Model fast');

	// Session 3: a misbehaving replay re-delivers seq 1–2. Await request 3
	// (cursor '3'), queue the replay — the producer filter and the consumer
	// watermark both drop every frame, so nothing changes.
	await waitForStreamRequests(3);
	queueStreamSession({
		frames: [
			{
				seq: 1,
				type: 'agent.delegation_planned',
				data: { children: [CHILD_A, CHILD_B], depth: 1 }
			},
			{
				seq: 2,
				type: 'agent.children_resolved',
				data: { children: [{ run_id: CHILD_A, state: 'succeeded' }] }
			}
		]
	});
	await expect(main.getByTestId('tree-cursor')).toContainText('Events applied: 3 (cursor #3)');
	await expect(main.getByTestId('child-row')).toHaveCount(2);

	// The subscriptions resumed from the watermark each time: cursors
	// null → '1' → '3' for the first three requests.
	await expect
		.poll(
			async () =>
				(await callsOf(page, 'streamConversationEvents')).slice(0, 3).map((call) => call.cursor ?? null),
			{ timeout: 15000 }
		)
		.toEqual([null, '1', '3']);
});

test('output_delta bursts never reread; a transition still refreshes exactly once', async ({ page }) => {
	await openFixture(page, 'empty');
	const main = page.getByTestId('component-host').first();

	// Session 1 (on the held mount subscription): a high-frequency fold-only
	// delta burst. Every frame is applied to the fold and counted by the
	// cursor — the burst is OBSERVED, never dropped — but none of it can
	// change the replayed rows, so none of it justifies a read.
	queueStreamSession({
		frames: Array.from({ length: 12 }, (_, index) => ({
			seq: index + 1,
			type: 'agent.output_delta',
			data: { round: 1, kind: 'text', part_index: 0, delta: `chunk ${index}` }
		}))
	});
	await waitForStreamRequests(1);
	await expect(main.getByTestId('tree-cursor')).toContainText('Events applied: 12 (cursor #12)');

	// The closed session reconnects (cursor '12') and is held; the quiet
	// window spans several debounce windows: the burst produced ZERO reads
	// beyond the single mount read.
	await waitForStreamRequests(2);
	await page.waitForTimeout(600);
	expect((await callsOf(page, 'readConversation')).length).toBe(1);

	// Session 2 (on the held reconnect): a durable transition BETWEEN two
	// deltas, plus a second transition inside the same debounce window.
	// Exactly ONE reread lands; the deltas and the coalesced second
	// transition add nothing.
	queueStreamSession({
		frames: [
			{
				seq: 13,
				type: 'agent.output_delta',
				data: { round: 1, kind: 'text', part_index: 0, delta: 'x' }
			},
			{ seq: 14, type: 'run.transition', data: { from: 'running', to: 'waiting_children' } },
			{
				seq: 15,
				type: 'agent.output_delta',
				data: { round: 1, kind: 'text', part_index: 0, delta: 'y' }
			},
			{ seq: 16, type: 'run.transition', data: { from: 'waiting_children', to: 'running' } }
		]
	});
	await expect
		.poll(async () => (await callsOf(page, 'readConversation')).length, { timeout: 10000 })
		.toBe(2);
	await expect(main.getByTestId('tree-cursor')).toContainText('Events applied: 16 (cursor #16)');
	// The refreshed root stays honest: the fixture row never changed state.
	await expect(main.getByTestId('root-state')).toHaveAttribute('data-state', 'active');
	await page.waitForTimeout(600);
	expect((await callsOf(page, 'readConversation')).length).toBe(2);
});

test('bounded outcomes are distinguishable; cancel posts the explicit tree-cancel and renders honest states', async ({ page }) => {
	await openFixture(page, 'states');
	const main = page.getByTestId('component-host').first();
	const rows = main.getByTestId('child-row');
	await expect(rows).toHaveCount(3);

	// Nested child failure.
	await expect(rows.nth(0)).toHaveAttribute('data-run-id', CHILD_A);
	await expect(main.getByTestId('child-state').nth(0)).toHaveAttribute('data-state', 'failed');
	await expect(main.getByTestId('child-state').nth(0)).toHaveText('Failed');

	// Cancellation is its own state, never folded into failure.
	await expect(rows.nth(1)).toHaveAttribute('data-run-id', CHILD_B);
	await expect(main.getByTestId('child-state').nth(1)).toHaveAttribute('data-state', 'cancelled');
	await expect(main.getByTestId('child-state').nth(1)).toHaveText('Cancelled (verified)');

	// Denied grant: the effective set clamped to empty — a third, distinct state.
	await expect(rows.nth(2)).toHaveAttribute('data-run-id', CHILD_C);
	await expect(main.getByTestId('child-state').nth(2)).toHaveAttribute('data-state', 'succeeded');
	const denied = main.getByTestId('child-grants-none');
	await expect(denied).toHaveCount(1);
	await expect(denied).toContainText('no effective grants');
	await expect(main.getByTestId('child-grants').nth(2)).toHaveAttribute('data-grants', 'none');
	await expect(main.getByTestId('child-grants').nth(0)).toHaveAttribute('data-grants', 'some');

	// Cancel: the control posts the explicit tree-cancel body; the honest
	// pending answer never claims a verified outcome.
	await main.getByTestId('cancel-conversation').click();
	await expect
		.poll(async () =>
			(await callsOf(page, 'cancelConversation')).map((call) => call.payload)
		)
		.toEqual([{ cancel_children: true }]);
	await expect(main.getByTestId('cancel-status')).toHaveAttribute('data-outcome', 'pending');
	await expect(main.getByTestId('cancel-status')).toHaveText(
		'Cancellation requested — not yet verified.'
	);
	// Requested ≠ verified: the root badge stays the replayed running state.
	await expect(main.getByTestId('root-state')).toContainText('Running');
	await expect(main.getByTestId('root-cancel-flag')).toBeVisible();

	// The engine lands the cancellation; re-entry reports the KNOWN outcome.
	await page.evaluate(() => {
		window.__maiDelegationApi?.armCancel('cancelled');
	});
	await main.getByTestId('cancel-conversation').click();
	await expect(main.getByTestId('cancel-status')).toHaveAttribute('data-outcome', 'cancelled');
	await expect(main.getByTestId('cancel-status')).toHaveText('Cancelled (verified).');
	await expect(main.getByTestId('root-state')).toHaveAttribute('data-state', 'cancelled');
	// Terminal tree: the cancel control is gone.
	await expect(main.getByTestId('cancel-conversation')).toHaveCount(0);
	await expect(main.getByTestId('cancel-unavailable')).toBeVisible();
});

test('budget consumption folds the ledger: reservations, charges stay, releases refund outstanding', async ({ page }) => {
	await openFixture(page, 'empty');
	const main = page.getByTestId('component-host').first();

	// Session 1: reserve both children, charge both, release one. The charged
	// spend stays; the release refunds only the outstanding reservation. A
	// duplicate seq 2 in the same session must not double-count.
	queueStreamSession({
		frames: [
			{
				seq: 1,
				type: 'agent.budget_reserved',
				data: { owner: ROOT, children: [CHILD_A, CHILD_B] }
			},
			{ seq: 2, type: 'agent.budget_charged', data: { run: CHILD_A, steps: 2, tokens: 100 } },
			{ seq: 2, type: 'agent.budget_charged', data: { run: CHILD_A, steps: 2, tokens: 100 } },
			{ seq: 3, type: 'agent.budget_charged', data: { run: CHILD_B, steps: 1, tokens: 50 } },
			{
				seq: 4,
				type: 'agent.budget_released',
				data: { owner: ROOT, children: [CHILD_B] }
			}
		]
	});
	await waitForStreamRequests(1);
	await expect(main.getByTestId('budget-summary')).toBeVisible();
	await expect(main.getByTestId('budget-active')).toHaveText('Active reservations: 1');
	await expect(main.getByTestId('budget-steps')).toHaveText('Steps charged: 3');
	await expect(main.getByTestId('budget-tokens')).toHaveText('Tokens charged: 150');
	await expect(main.getByTestId('budget-ledger')).toHaveText('Reservations: 1 · Releases: 1');
	// The duplicate frame was dropped by the watermark: cursor #4, three applied.
	await expect(main.getByTestId('tree-cursor')).toContainText('Events applied: 4 (cursor #4)');
});

test('fail-closed without services; read failure recovers; unmount and terminal tree release the stream', async ({ page }) => {
	await openFixture(page, 'default');

	// BARE host: no bag ⇒ visible fail-closed state, zero requests of its own.
	const bare = page.getByTestId('component-host').nth(1);
	await expect(bare.getByTestId('tree-config-error')).toContainText(
		'without its delegation service'
	);

	// Read failure: the refresh surfaces the stable envelope code, never
	// all-green; Retry recovers.
	const main = page.getByTestId('component-host').first();
	await page.evaluate(() => {
		window.__maiDelegationApi?.failNextRead({
			status: 503,
			code: 'unavailable',
			message: 'the facade is restarting'
		});
	});
	queueStreamSession({ frames: [{ seq: 1, type: 'run.transition', data: {} }] });
	await waitForStreamRequests(1);
	const refreshError = main.getByTestId('refresh-error');
	await expect(refreshError).toBeVisible();
	await expect(refreshError).toHaveAttribute('data-error-code', 'unavailable');
	await main.getByTestId('refresh-retry').click();
	await expect(refreshError).toBeHidden();

	// Unmount releases the subscription: no session is consumed afterwards.
	const requestsBefore = streamRequests.length;
	await page.evaluate(() => {
		window.__maiDelegationApi?.toggleMain(false);
	});
	await expect(page.getByTestId('component-host')).toHaveCount(1);
	await page.waitForTimeout(1200);
	expect(streamRequests.length).toBe(requestsBefore);

	// Remount starts clean (no fold leakage between mount cycles).
	await page.evaluate(() => {
		window.__maiDelegationApi?.toggleMain(true);
	});
	await waitForStreamRequests(requestsBefore + 1);
	const remounted = page.getByTestId('component-host').first();
	await expect(remounted.getByTestId('tree-cursor')).toContainText('Events applied: 0');
	await expect(remounted.getByTestId('child-row')).toHaveCount(2);

	// Terminal tree: nothing new can arrive; the loop closes the stream and
	// stops subscribing (C05 rule 2).
	await page.evaluate(({ rows }) => {
		const api = window.__maiDelegationApi;
		api?.setRootState('succeeded');
		api?.setChildren(rows);
	}, {
		rows: [
			{
				run_id: CHILD_A,
				server_id: SERVER,
				parent_run_id: ROOT,
				depth: 1,
				profile_revision: 3,
				prompt_revision: 1,
				model_profile: 'fast',
				requested_grants: ['fixture.read'],
				state: 'succeeded',
				cancel_requested_at: null,
				submitted_at: '2026-10-16T09:02:00Z',
				updated_at: '2026-10-16T09:03:00Z'
			},
			{
				run_id: CHILD_B,
				server_id: SERVER,
				parent_run_id: ROOT,
				depth: 1,
				profile_revision: 3,
				prompt_revision: 1,
				model_profile: 'fast',
				requested_grants: ['fixture.read'],
				state: 'succeeded',
				cancel_requested_at: null,
				submitted_at: '2026-10-16T09:02:00Z',
				updated_at: '2026-10-16T09:03:00Z'
			}
		]
	});
	// One more frame triggers the durable refresh that reveals the terminal
	// tree; it travels on the CURRENT (held) session — no new request is made.
	queueStreamSession({ frames: [{ seq: 2, type: 'run.transition', data: {} }] });
	await expect(remounted.getByTestId('stream-status')).toContainText('Event stream closed.', {
		timeout: 15000
	});
	await expect(remounted.getByTestId('tree-cursor')).toContainText('Events applied: 1 (cursor #2)');
	// The quiet tree never subscribes again (C05 rule 2: nothing new can arrive).
	const stable = streamRequests.length;
	expect(stable).toBe(requestsBefore + 1);
	await page.waitForTimeout(1200);
	expect(streamRequests.length).toBe(stable);
});
