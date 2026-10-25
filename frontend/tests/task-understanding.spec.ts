import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { TASK_UNDERSTANDING_PAGE_LIMIT } from '../src/lib/workspace/entries/07-web-tasks';

/**
 * act `17-understanding-ui` — real-browser proofs for the compact task
 * understanding presentation (`TaskUnderstanding.svelte` through the REAL
 * `07-web-tasks.ts` service boundary):
 *
 * - U1: at 360x640 the ready block renders scope/constraints/questions/plan
 *   with bounded pages, "Showing n of m" lines and "Load more" controls
 *   inside `overflow-y: auto` containers — no horizontal overflow, appended
 *   pages are deduplicated by stable id.
 * - U2: every degraded state is explicit — an empty task records gaps ("no
 *   summary/constraints/steps recorded"), the scope-blind 404 renders the
 *   "understanding unavailable" label, a transport error renders its real
 *   code, and an unbound services bag fails closed with zero fetches. The
 *   open-questions section ALWAYS renders the explicit "no open questions
 *   recorded" state (the §8 wire exposes no questions collection; nothing
 *   is invented).
 * - Criterion 2 (reopen/reconnect): remounting the block re-derives the
 *   SAME understanding from server truth with read-only calls only — zero
 *   mutation calls and zero browser-storage access ever.
 */

const FIXTURE_URL = '/mai-task-understanding-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-task-understanding-app.svelte';
const ENTRY_ID = 'virtual:mai-task-understanding-entry.js';

const COMPONENT = `${FRONTEND_ROOT}src/lib/panels/chat/TaskUnderstanding.svelte`;
const TASKS_ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/07-web-tasks.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const CONVERSATION_ID = 'b7c2d3e4-0000-4000-8000-000000000123';
const TASK_ID = 'f70a0000-1111-4222-8333-444455556666';

type Scenario = 'ready' | 'empty' | 'absent' | 'error' | 'unbound';

interface RecordedCall {
	op: string;
	payload?: unknown;
}

interface TaskWindowApi {
	calls: RecordedCall[];
	remount(): void;
}

declare global {
	interface Window {
		/** Unique per-spec names: other specs declare their own Window globals. */
		__maiUnderstandingApi?: TaskWindowApi;
		__maiUnderstandingScenario?: Scenario;
		__maiUnderstandingStorage?: string[];
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

async function callsOf(page: Page, op: string): Promise<RecordedCall[]> {
	return page.evaluate((operation) => {
		const api = window.__maiUnderstandingApi;
		return api ? api.calls.filter((call) => call.op === operation) : [];
	}, op);
}

// ---------------------------------------------------------------------------
// Fixture app (virtual module; runes mode like the reviewed harnesses)
// ---------------------------------------------------------------------------

const APP_SOURCE = `
<script>
	import TaskUnderstanding from '${COMPONENT}';
	import ComponentHost from '${HOST}';
	import { ApiError } from '${CLIENT}';
	import { taskServices, TASK_UNDERSTANDING_PAGE_LIMIT } from '${TASKS_ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const CONVERSATION_ID = '${CONVERSATION_ID}';
	const TASK_ID = '${TASK_ID}';

	const scenario = window.__maiUnderstandingScenario ?? 'ready';
	const calls = [];

	// --- wire fixtures (exact §8 shapes) -------------------------------------

	function headerFor() {
		const empty = scenario === 'empty';
		return {
			task_id: TASK_ID,
			conversation_id: CONVERSATION_ID,
			server_id: SERVER_A,
			revision: empty ? 1 : 4,
			title: 'Renew the TLS certificate on edge-01',
			summary: empty
				? null
				: 'Replace the expiring edge-01 certificate and verify the deployment end to end.',
			requested_phase: 'inspect',
			status: 'in_progress',
			active_step_id: empty ? null : 's2',
			counts: {
				steps_total: empty ? 0 : 12,
				steps_executed: empty ? 0 : 3,
				constraints_total: empty ? 0 : 12,
				evidence_total: empty ? 0 : 5,
				criteria_total: empty ? 0 : 2,
				criteria_verified: empty ? 0 : 1
			},
			created_at: '2026-10-16T08:00:00Z',
			updated_at: '2026-10-16T09:00:00Z'
		};
	}

	function constraintRow(index) {
		return {
			task_id: TASK_ID,
			constraint_id: 'c' + index,
			category: index % 3 === 0 ? 'safety' : index % 3 === 1 ? 'scope' : 'procedure',
			statement: 'Constraint ' + index + ': stay inside the approved maintenance window.',
			origin: index === 1 ? 'user' : 'model',
			source_message_refs: index === 1 ? ['m-1'] : [],
			position: index,
			superseded_at: null,
			superseded_by: null,
			created_at: '2026-10-16T08:05:00Z',
			updated_at: '2026-10-16T08:05:00Z'
		};
	}

	function stepRow(index) {
		const executed = index === 1 || index === 3 || index === 4;
		const active = index === 2;
		return {
			task_id: TASK_ID,
			step_id: 's' + index,
			title: 'Step ' + index,
			status: executed ? 'executed' : active ? 'in_progress' : 'pending',
			verification: executed ? 'passed' : 'not_checked',
			position: index,
			risk: index % 2 === 0 ? 'read_only' : 'mutating',
			prerequisite_unresolved: index === 7,
			expected_check: 'health endpoint returns 200',
			skip_reason: null,
			result_summary: executed ? 'recorded observation obs-' + index : null,
			evidence_refs: executed
				? [
						{
							kind: 'message',
							evidence_id: 'd4e5f6a7-0000-4000-8000-' + String(index).padStart(12, '0'),
							note: null
						}
					]
				: [],
			depends_on: index > 1 ? ['s' + (index - 1)] : [],
			superseded_at: null,
			superseded_by: null,
			created_at: '2026-10-16T08:06:00Z',
			updated_at: '2026-10-16T08:07:00Z'
		};
	}

	function pageOf(rows, limit, offset) {
		const sliced = rows.slice(offset, offset + limit);
		return {
			task_id: TASK_ID,
			limit,
			offset,
			total: rows.length,
			has_more: offset + sliced.length < rows.length,
			items: sliced
		};
	}

	// --- the REAL service boundary over a recording api slice ----------------

	const api = {
		async getConversationTask(conversationId, serverId) {
			calls.push({ op: 'getConversationTask', payload: { conversationId, serverId } });
			if (scenario === 'absent') {
				throw new ApiError(404, 'task_not_found', 'no active task episode in this server scope');
			}
			if (conversationId !== CONVERSATION_ID || serverId !== SERVER_A) {
				throw new ApiError(404, 'task_not_found', 'scope-blind 404');
			}
			return headerFor();
		},
		async listTaskConstraints(taskId, serverId, query) {
			calls.push({ op: 'listTaskConstraints', payload: { taskId, serverId, query } });
			if (scenario === 'error') {
				throw new ApiError(503, 'unavailable', 'task store is down');
			}
			const empty = scenario === 'empty';
			const rows = empty ? [] : Array.from({ length: 12 }, (_, i) => constraintRow(i + 1));
			// The load-more page deliberately re-sends the LAST row of the
			// previous page (an overlapping window) so the component's dedup
			// is proven while the missing rows still arrive.
			const limit = query?.limit ?? ${JSON.stringify(TASK_UNDERSTANDING_PAGE_LIMIT)};
			const offset = query?.offset ?? 0;
			const page = pageOf(rows, limit, offset);
			if (!empty && offset > 0 && page.items.length > 0) {
				page.items = [rows[offset - 1], ...page.items];
			}
			return page;
		},
		async listTaskSteps(taskId, serverId, query) {
			calls.push({ op: 'listTaskSteps', payload: { taskId, serverId, query } });
			const rows = scenario === 'empty' ? [] : Array.from({ length: 12 }, (_, i) => stepRow(i + 1));
			return pageOf(rows, query?.limit ?? ${JSON.stringify(TASK_UNDERSTANDING_PAGE_LIMIT)}, query?.offset ?? 0);
		},
		// Mutations exist on the boundary but the understanding block is
		// read-only: recording + throwing makes ANY call a loud test failure.
		async submitTaskCorrection(taskId, payload) {
			calls.push({ op: 'submitTaskCorrection', payload });
			throw new Error('mutation is never expected from the understanding block');
		},
		async submitTaskDecision(taskId, payload) {
			calls.push({ op: 'submitTaskDecision', payload });
			throw new Error('mutation is never expected from the understanding block');
		}
	};

	const services = taskServices(api);
	let show = $state(true);

	function remount() {
		show = false;
		setTimeout(() => {
			show = true;
		}, 0);
	}

	window.__maiUnderstandingApi = {
		calls,
		remount
	};
</script>

<section data-testid="host-main" aria-label="Host main">
	<ComponentHost label="Host main">
		{#if show}
			<TaskUnderstanding
				services={scenario === 'unbound' ? null : services}
				conversationId={CONVERSATION_ID}
				serverId={SERVER_A}
			/>
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

// ---------------------------------------------------------------------------

function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-task-understanding-harness',
		resolveId(id) {
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
			outDir: 'node_modules/.mai-task-understanding-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'task-understanding-[hash].js',
					assetFileNames: 'demo[extname]'
				}
			}
		}
	});

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
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAI task understanding fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"></script>
</body>
</html>`;

	// Storage honesty guard (P5): the understanding block must NEVER touch
	// browser storage; any access is recorded and asserted zero.
	await page.addInitScript(() => {
		window.__maiUnderstandingStorage = [];
		for (const kind of ['localStorage', 'sessionStorage'] as const) {
			Object.defineProperty(window, kind, {
				configurable: true,
				get() {
					window.__maiUnderstandingStorage?.push('get ' + kind);
					return undefined;
				},
				set() {
					window.__maiUnderstandingStorage?.push('set ' + kind);
				}
			});
		}
	});

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
	page.on('pageerror', (error) => {
		throw new Error(`fixture page error: ${error.message}`);
	});
});

async function openFixture(page: Page, scenario: Scenario): Promise<void> {
	await page.addInitScript((value) => {
		window.__maiUnderstandingScenario = value;
	}, scenario);
	await page.goto(FIXTURE_URL);
}

// --- rows -------------------------------------------------------------------

test('U1: ready task renders bounded scope/constraints/plan pages with dedup load-more at 360x640', async ({
	page
}) => {
	await page.setViewportSize({ width: 360, height: 640 });
	await openFixture(page, 'ready');

	const block = page.getByTestId('task-understanding');
	await expect(block).toHaveAttribute('data-state', 'ready');

	// Scope section: server-backed header only.
	await expect(page.getByTestId('task-understanding-scope-title')).toHaveText(
		'Renew the TLS certificate on edge-01'
	);
	await expect(page.getByTestId('task-understanding-scope-summary')).toContainText(
		'Replace the expiring edge-01 certificate'
	);
	await expect(page.getByTestId('task-understanding-scope-meta')).toContainText(
		'phase inspect (advisory) · in progress · rev 4'
	);
	await expect(page.getByTestId('task-understanding-scope-meta')).toContainText(
		'steps 3/12 executed'
	);

	// The §8 wire exposes no questions collection: the explicit gap state.
	await expect(page.getByTestId('task-understanding-questions')).toContainText(
		'No open questions recorded.'
	);

	// Bounded first pages with honest counts.
	await expect(page.getByTestId('task-understanding-constraint-row')).toHaveCount(10);
	await expect(page.getByTestId('task-understanding-constraints-meta')).toHaveText(
		'Showing 10 of 12 constraints.'
	);
	await expect(page.getByTestId('task-understanding-step-row')).toHaveCount(10);
	await expect(page.getByTestId('task-understanding-plan-meta')).toHaveText(
		'Showing 10 of 12 steps.'
	);

	// Bounded scroll containers: overflow-y auto + a real scrollbar before Load more.
	for (const list of ['task-understanding-constraint-list', 'task-understanding-step-list']) {
		const box = await page.getByTestId(list).evaluate((el) => {
			const style = window.getComputedStyle(el);
			return {
				overflowY: style.overflowY,
				maxHeight: style.maxHeight,
				scrollHeight: el.scrollHeight,
				clientHeight: el.clientHeight
			};
		});
		expect(box.overflowY).toBe('auto');
		expect(box.maxHeight).not.toBe('none');
		expect(box.scrollHeight).toBeGreaterThan(box.clientHeight);
	}

	// Load more constraints: the overlapping second page is deduplicated by id.
	await page.getByTestId('task-understanding-constraints-more').click();
	await expect(page.getByTestId('task-understanding-constraint-row')).toHaveCount(12);
	await expect(page.getByTestId('task-understanding-constraints-meta')).toHaveText(
		'Showing 12 of 12 constraints.'
	);
	await expect(page.getByTestId('task-understanding-constraints-more')).toHaveCount(0);

	// Load more steps: same bounded page + dedup behavior.
	await page.getByTestId('task-understanding-plan-more').click();
	await expect(page.getByTestId('task-understanding-step-row')).toHaveCount(12);
	await expect(page.getByTestId('task-understanding-plan-meta')).toHaveText('Showing 12 of 12 steps.');
	await expect(page.getByTestId('task-understanding-plan-more')).toHaveCount(0);

	// Step honesty: active step marker, verification and risk rendered as text.
	const activeRow = page
		.getByTestId('task-understanding-step-row')
		.filter({ hasText: 'Step 2' })
		.first();
	await expect(activeRow).toContainText('(active)');
	await expect(activeRow).toContainText('in progress · not checked · read_only');
	const unresolvedRow = page
		.getByTestId('task-understanding-step-row')
		.filter({ hasText: 'Step 7' })
		.first();
	await expect(unresolvedRow).toContainText('prerequisite unresolved');

	// U1: no horizontal overflow at 360x640.
	const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
	expect(scrollWidth).toBeLessThanOrEqual(360);
});

test('U2: an empty task renders every explicit gap and invents nothing', async ({ page }) => {
	await openFixture(page, 'empty');

	const block = page.getByTestId('task-understanding');
	await expect(block).toHaveAttribute('data-state', 'ready');
	await expect(page.getByTestId('task-understanding-scope-summary')).toHaveText(
		'No summary recorded.'
	);
	await expect(page.getByTestId('task-understanding-constraints-empty')).toHaveText(
		'No constraints recorded.'
	);
	await expect(page.getByTestId('task-understanding-plan-empty')).toHaveText(
		'No plan steps recorded.'
	);
	await expect(page.getByTestId('task-understanding-questions')).toContainText(
		'No open questions recorded.'
	);
	await expect(page.getByTestId('task-understanding-scope-meta')).toContainText(
		'steps 0/0 executed · constraints 0'
	);
	await expect(page.getByTestId('task-understanding-constraints-more')).toHaveCount(0);
	await expect(page.getByTestId('task-understanding-plan-more')).toHaveCount(0);
});

test('U2: the scope-blind 404 renders understanding unavailable, never fabricated scope', async ({
	page
}) => {
	await openFixture(page, 'absent');

	const block = page.getByTestId('task-understanding');
	await expect(block).toHaveAttribute('data-state', 'unavailable');
	await expect(page.getByTestId('task-understanding-unavailable')).toContainText(
		'Task understanding unavailable.'
	);
	await expect(page.getByTestId('task-understanding-scope')).toHaveCount(0);
	await expect(page.getByTestId('task-understanding-constraint-row')).toHaveCount(0);
});

test('U2: a transport failure renders the real code and no invented data', async ({ page }) => {
	await openFixture(page, 'error');

	const block = page.getByTestId('task-understanding');
	await expect(block).toHaveAttribute('data-state', 'error');
	await expect(page.getByTestId('task-understanding-error')).toContainText(
		'Task understanding could not be loaded.'
	);
	await expect(page.getByTestId('task-understanding-error-code')).toHaveText('unavailable');
	await expect(page.getByTestId('task-understanding-scope')).toHaveCount(0);
});

test('fail-closed: an unbound services bag renders the explicit state with zero fetches', async ({
	page
}) => {
	await openFixture(page, 'unbound');

	const block = page.getByTestId('task-understanding');
	await expect(block).toHaveAttribute('data-state', 'unbound');
	await expect(page.getByTestId('task-understanding-unbound')).toContainText(
		'No task services are bound; nothing is fetched or fabricated.'
	);
	expect(await callsOf(page, 'getConversationTask')).toHaveLength(0);
	expect(await callsOf(page, 'listTaskConstraints')).toHaveLength(0);
});

test('criterion 2: remount (reopen) re-derives the same understanding read-only with zero storage access', async ({
	page
}) => {
	await openFixture(page, 'ready');

	const block = page.getByTestId('task-understanding');
	await expect(block).toHaveAttribute('data-state', 'ready');
	await expect(page.getByTestId('task-understanding-constraint-row')).toHaveCount(10);

	// Simulated reopen: destroy and recreate the block; the server re-derives
	// the SAME understanding (no localStorage canonical persistence).
	await page.evaluate(() => window.__maiUnderstandingApi?.remount());
	await expect
		.poll(async () => (await callsOf(page, 'getConversationTask')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(2);
	await expect(block).toHaveAttribute('data-state', 'ready');
	await expect(page.getByTestId('task-understanding-scope-title')).toHaveText(
		'Renew the TLS certificate on edge-01'
	);
	await expect(page.getByTestId('task-understanding-constraints-meta')).toHaveText(
		'Showing 10 of 12 constraints.'
	);

	// Read-only proof: only the three reads were ever called; the recorded
	// mutation stubs were never touched.
	const ops = await page.evaluate(() => (window.__maiUnderstandingApi?.calls ?? []).map((c) => c.op));
	for (const op of ops) {
		expect(['getConversationTask', 'listTaskConstraints', 'listTaskSteps']).toContain(op);
	}
	expect(await callsOf(page, 'submitTaskCorrection')).toHaveLength(0);
	expect(await callsOf(page, 'submitTaskDecision')).toHaveLength(0);

	// Storage honesty: zero localStorage/sessionStorage access, ever.
	const storageAccess = await page.evaluate(() => window.__maiUnderstandingStorage ?? []);
	expect(storageAccess).toEqual([]);
});
