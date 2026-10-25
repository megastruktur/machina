import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * act `18-plan-ui` — real-browser proofs for the §8 Plan section
 * (`TaskPlan.svelte` through the REAL `07-web-tasks.ts` service boundary):
 *
 * - Criterion 1: bounded/deduped Load more pages (steps, evidence, revision
 *   ledger), live updates/reconnect (bounded poll + quiet absent-probe),
 *   stable item IDs across server-side updates, and STALE revision
 *   handling — a stale `base_revision` surfaces the honest structured
 *   `409 revision_conflict` (nothing overwritten, no fake success), and a
 *   user edit always PROPOSES a revision through `submitTaskCorrection`
 *   (append-only ledger; nothing rewritten or deleted in place).
 * - Criterion 2: `executed` and `verified` render as two visually distinct
 *   badges (different classes AND different computed background, always
 *   text-labelled — never one shared "done" badge); removing a RUNNING
 *   item states the honest disposition (a proposal that does not stop any
 *   process); at 360x640 controls stay usable with no horizontal overflow.
 * - U2/P5: empty/scope-blind-404/error/unbound states are explicit; the
 *   unanchored proposal form is honestly disabled; remount (reopen)
 *   re-derives server truth read-only with zero browser-storage access.
 */

const FIXTURE_URL = '/mai-task-plan-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-task-plan-app.svelte';
const ENTRY_ID = 'virtual:mai-task-plan-entry.js';

const COMPONENT = `${FRONTEND_ROOT}src/lib/panels/chat/TaskPlan.svelte`;
const TASKS_ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/07-web-tasks.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const CONVERSATION_ID = 'b7c2d3e4-0000-4000-8000-000000000123';
const TASK_ID = 'f70a0000-2222-4222-8333-444455556666';
const ANCHOR = 'e1000000-0000-4000-8000-000000000001';

type Scenario = 'ready' | 'empty' | 'absent' | 'error' | 'unbound' | 'unanchored';

interface RecordedCall {
	op: string;
	payload?: unknown;
}

/**
 * The exact correction body the fixture app authors and records (a test
 * seam shape we control end to end; asserted field-for-field below).
 */
interface SubmittedCorrection {
	server_id: string;
	patch: {
		schema_version: number;
		source_message_id: string;
		base_revision: number;
		idempotency_key: string;
		rationale: string | null;
		operations: Array<Record<string, unknown>>;
	};
}

interface TaskPlanWindowApi {
	calls: RecordedCall[];
	remount(): void;
	/** Server-side control: the revision moves WITHOUT the UI acting (P5). */
	bumpRevision(next: number): void;
	/** Server-side control: the executor completes an item (P5 live update). */
	executorCompleteStep(stepId: string): void;
	/** Server-side control: an episode is created after the mount (absent probe). */
	seedEpisode(): void;
	/** Hold every submit in flight (pending/applied distinction proof). */
	holdSubmit(on: boolean): void;
}

declare global {
	interface Window {
		/** Unique per-spec names: other specs declare their own Window globals. */
		__maiTaskPlanApi?: TaskPlanWindowApi;
		__maiTaskPlanScenario?: Scenario;
		__maiTaskPlanPollMs?: number;
		__maiTaskPlanStorage?: string[];
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
		const api = window.__maiTaskPlanApi;
		return api ? api.calls.filter((call) => call.op === operation) : [];
	}, op);
}

// ---------------------------------------------------------------------------
// Fixture app (virtual module; runes mode like the reviewed harnesses)
// ---------------------------------------------------------------------------

const APP_SOURCE = `
<script>
	import TaskPlan from '${COMPONENT}';
	import ComponentHost from '${HOST}';
	import { ApiError } from '${CLIENT}';
	import { taskServices } from '${TASKS_ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const CONVERSATION_ID = '${CONVERSATION_ID}';
	const TASK_ID = '${TASK_ID}';
	const ANCHOR = '${ANCHOR}';

	const scenario = window.__maiTaskPlanScenario ?? 'ready';
	const pollMs = window.__maiTaskPlanPollMs ?? 0;
	const calls = [];

	// --- mutable server-side fixture state (the "real backend" stand-in) -----

	let hasTask = scenario !== 'absent';
	let revision = scenario === 'empty' ? 1 : 4;
	let stepSeq = 100;
	const supersededRows = [];

	function baseSteps() {
		if (scenario === 'empty') return [];
		const rows = [];
		const mk = (id, title, patch) => Object.assign({
			task_id: TASK_ID,
			step_id: id,
			title,
			status: 'pending',
			verification: 'not_checked',
			position: 0,
			risk: 'read_only',
			prerequisite_unresolved: false,
			expected_check: 'health endpoint returns 200',
			skip_reason: null,
			result_summary: null,
			evidence_refs: [],
			depends_on: [],
			superseded_at: null,
			superseded_by: null,
			created_at: '2026-10-16T08:06:00Z',
			updated_at: '2026-10-16T08:07:00Z'
		}, patch);
		rows.push(mk('s1', 'snapshot current state', {
			status: 'executed',
			verification: 'passed',
			position: 1,
			risk: 'read_only',
			result_summary: 'recorded observation obs-1',
			evidence_refs: [{ kind: 'message', evidence_id: 'd4e5f6a7-0000-4000-8000-000000000001', note: null }],
			updated_at: '2026-10-16T08:30:00Z'
		}));
		rows.push(mk('s2', 'apply the change', {
			status: 'in_progress',
			position: 2,
			risk: 'mutating'
		}));
		rows.push(mk('s3', 'execute the script', {
			status: 'executed',
			position: 3,
			risk: 'mutating',
			depends_on: ['s2']
		}));
		for (let i = 4; i <= 11; i++) {
			rows.push(mk('s' + i, 'step s' + i, { position: i, depends_on: ['s' + (i - 1)] }));
		}
		rows.push(mk('s12', 'optional cleanup', {
			status: 'skipped',
			position: 12,
			skip_reason: 'not needed for this episode',
			depends_on: ['s11']
		}));
		for (const [index, row] of rows.entries()) row.position = index + 1;
		return rows;
	}

	let steps = baseSteps();

	function baseEvidence() {
		if (scenario === 'empty') return [];
		return [
			{ task_id: TASK_ID, evidence_id: 'd4e5f6a7-0000-4000-8000-000000000001', kind: 'message', note: 'user request', origin_step_id: null, created_at: '2026-10-16T08:05:00Z' },
			{ task_id: TASK_ID, evidence_id: 'd4e5f6a7-0000-4000-8000-000000000002', kind: 'observation', note: 'health endpoint 200', origin_step_id: 's1', created_at: '2026-10-16T08:31:00Z' },
			{ task_id: TASK_ID, evidence_id: 'd4e5f6a7-0000-4000-8000-000000000003', kind: 'run_event', note: null, origin_step_id: 's3', created_at: '2026-10-16T08:40:00Z' },
			{ task_id: TASK_ID, evidence_id: 'd4e5f6a7-0000-4000-8000-000000000004', kind: 'artifact', note: 'report', origin_step_id: 's3', created_at: '2026-10-16T08:41:00Z' },
			{ task_id: TASK_ID, evidence_id: 'd4e5f6a7-0000-4000-8000-000000000005', kind: 'document', note: null, origin_step_id: null, created_at: '2026-10-16T08:42:00Z' }
		];
	}

	let evidenceRows = baseEvidence();

	function baseRevisions() {
		if (scenario === 'empty') {
			return [{ task_id: TASK_ID, revision: 1, kind: 'create', author: 'system', source_message_id: ANCHOR, item_id: null, payload: null, applied_at: '2026-10-16T08:04:00Z' }];
		}
		const rows = [];
		for (let rev = 12; rev >= 1; rev--) {
			rows.push({
				task_id: TASK_ID,
				revision: rev,
				kind: rev === 1 ? 'create' : rev % 2 === 0 ? 'plan_patch' : 'item_update',
				author: rev === 1 ? 'system' : rev % 3 === 0 ? 'user' : 'model',
				source_message_id: rev === 3 ? ANCHOR : null,
				item_id: rev > 1 ? 's' + ((rev % 12) + 1) : null,
				payload: null,
				applied_at: '2026-10-16T08:' + String(10 + rev).padStart(2, '0') + ':00Z'
			});
		}
		return rows;
	}

	let ledger = baseRevisions();

	function headerFor() {
		const active = steps.filter((row) => row.superseded_at === null);
		return {
			task_id: TASK_ID,
			conversation_id: CONVERSATION_ID,
			server_id: SERVER_A,
			revision,
			title: 'Renew the TLS certificate on edge-01',
			summary: scenario === 'empty' ? null : 'Replace the expiring certificate and verify the deployment.',
			requested_phase: 'inspect',
			status: 'in_progress',
			active_step_id: scenario === 'empty' ? null : 's2',
			counts: {
				steps_total: active.length,
				steps_executed: active.filter((row) => row.status === 'executed').length,
				constraints_total: scenario === 'empty' ? 0 : 2,
				evidence_total: evidenceRows.length,
				criteria_total: scenario === 'empty' ? 0 : 2,
				criteria_verified: scenario === 'empty' ? 0 : 1
			},
			created_at: '2026-10-16T08:00:00Z',
			updated_at: '2026-10-16T09:00:00Z'
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

	let holdSubmit = false;
	let holdResolve = null;

	const api = {
		async getConversationTask(conversationId, serverId) {
			calls.push({ op: 'getConversationTask', payload: { conversationId, serverId } });
			if (scenario === 'error') {
				throw new ApiError(503, 'unavailable', 'task store is down');
			}
			if (!hasTask) {
				throw new ApiError(404, 'task_not_found', 'no active task episode in this server scope');
			}
			if (conversationId !== CONVERSATION_ID || serverId !== SERVER_A) {
				throw new ApiError(404, 'task_not_found', 'scope-blind 404');
			}
			return headerFor();
		},
		async listTaskSteps(taskId, serverId, query) {
			calls.push({ op: 'listTaskSteps', payload: { taskId, serverId, query } });
			const active = steps
				.filter((row) => row.superseded_at === null)
				.slice()
				.sort((a, b) => a.position - b.position || (a.step_id < b.step_id ? -1 : 1));
			const limit = query?.limit ?? 10;
			const offset = query?.offset ?? 0;
			const page = pageOf(active, limit, offset);
			if (offset > 0 && page.items.length > 0) {
				// An overlapping AND within-page-duplicated window: the component
				// must dedup across pages AND inside one incoming page.
				page.items = [active[offset - 1], ...page.items, page.items[0]];
			}
			return page;
		},
		async listTaskEvidence(taskId, serverId, page) {
			calls.push({ op: 'listTaskEvidence', payload: { taskId, serverId, page } });
			return pageOf(evidenceRows, page?.limit ?? 10, page?.offset ?? 0);
		},
		async listTaskRevisions(taskId, serverId, page) {
			calls.push({ op: 'listTaskRevisions', payload: { taskId, serverId, page } });
			return pageOf(ledger, page?.limit ?? 10, page?.offset ?? 0);
		},
		async submitTaskCorrection(taskId, payload) {
			calls.push({ op: 'submitTaskCorrection', payload: JSON.parse(JSON.stringify(payload)) });
			if (holdSubmit) {
				const held = Promise.withResolvers();
				holdResolve = held.resolve;
				await held.promise;
			}
			if (payload.patch.base_revision !== revision) {
				throw new ApiError(409, 'revision_conflict', 'expected task revision does not match the current revision');
			}
			for (const operation of payload.patch.operations) {
				const step = operation.step;
				if (operation.op === 'add') {
					stepSeq += 1;
					steps.push({
						task_id: TASK_ID,
						step_id: step.step_id,
						title: step.title,
						status: step.status,
						verification: step.verification,
						position: steps.filter((row) => row.superseded_at === null).length + 1,
						risk: step.risk,
						prerequisite_unresolved: false,
						expected_check: null,
						skip_reason: null,
						result_summary: null,
						evidence_refs: [],
						depends_on: [],
						superseded_at: null,
						superseded_by: null,
						created_at: '2026-10-16T09:10:00Z',
						updated_at: '2026-10-16T09:10:00Z'
					});
					continue;
				}
				const targetId = operation.op === 'supersede' ? operation.supersedes_step_id : step.step_id;
				const current = steps.find((row) => row.step_id === targetId && row.superseded_at === null);
				if (current === undefined) {
					throw new ApiError(422, 'invalid_task_patch', 'operation target step does not exist in the active plan');
				}
				if (operation.op === 'update') {
					current.title = step.title;
					current.updated_at = '2026-10-16T09:10:00Z';
					continue;
				}
				// supersede: the old row stays RECORDED (P3), never deleted.
				current.superseded_at = '2026-10-16T09:10:00Z';
				current.superseded_by = step.step_id;
				supersededRows.push(current.step_id);
				stepSeq += 1;
				steps.push({
					task_id: TASK_ID,
					step_id: step.step_id,
					title: step.title,
					status: step.status,
					verification: current.verification,
					position: current.position,
					risk: current.risk,
					prerequisite_unresolved: false,
					expected_check: current.expected_check,
					skip_reason: step.skip_reason,
					result_summary: null,
					evidence_refs: current.evidence_refs.map((ref) => Object.assign({}, ref)),
					depends_on: current.depends_on.slice(),
					superseded_at: null,
					superseded_by: null,
					created_at: '2026-10-16T09:10:00Z',
					updated_at: '2026-10-16T09:10:00Z'
				});
			}
			revision += 1;
			const first = payload.patch.operations[0];
			ledger.unshift({
				task_id: TASK_ID,
				revision,
				kind: 'plan_patch',
				author: 'user',
				source_message_id: payload.patch.source_message_id,
				item_id: first.op === 'add' ? null : (first.op === 'supersede' ? first.supersedes_step_id : first.step.step_id),
				payload: null,
				applied_at: '2026-10-16T09:10:00Z'
			});
			return { task: headerFor(), revision: ledger[0], disposition: 'created' };
		}
	};

	const services = taskServices(api);
	let show = $state(true);

	function remount() {
		show = false;
		setTimeout(() => { show = true; }, 0);
	}

	window.__maiTaskPlanApi = {
		calls,
		remount,
		bumpRevision(next) {
			revision = next;
			ledger.unshift({
				task_id: TASK_ID, revision: next, kind: 'metadata', author: 'system',
				source_message_id: null, item_id: null, payload: null,
				applied_at: '2026-10-16T09:20:00Z'
			});
		},
		executorCompleteStep(stepId) {
			const row = steps.find((candidate) => candidate.step_id === stepId);
			if (row === undefined) throw new Error('no such step');
			row.status = 'executed';
			row.verification = 'passed';
			row.evidence_refs = [{ kind: 'observation', evidence_id: 'd4e5f6a7-0000-4000-8000-000000000009', note: null }];
			row.updated_at = '2026-10-16T09:25:00Z';
			revision += 1;
			ledger.unshift({
				task_id: TASK_ID, revision, kind: 'item_update', author: 'model',
				source_message_id: null, item_id: stepId, payload: null,
				applied_at: '2026-10-16T09:25:00Z'
			});
		},
		seedEpisode() {
			hasTask = true;
			steps = baseSteps().slice(0, 3);
			evidenceRows = baseEvidence().slice(0, 1);
			ledger = baseRevisions().slice(0, 1);
		},
		holdSubmit(on) {
			holdSubmit = on;
			if (!on && holdResolve !== null) {
				const resolve = holdResolve;
				holdResolve = null;
				resolve(undefined);
			}
		}
	};
</script>

<section data-testid="host-main" aria-label="Host main">
	<ComponentHost label="Host main">
		{#if show}
			<TaskPlan
				services={scenario === 'unbound' ? null : services}
				conversationId={CONVERSATION_ID}
				serverId={SERVER_A}
				sourceMessageId={scenario === 'unanchored' ? null : ANCHOR}
				pollMs={pollMs}
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
		name: 'mai-task-plan-harness',
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
			outDir: 'node_modules/.mai-task-plan-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'task-plan-[hash].js',
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
<title>MAI task plan fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"></script>
</body>
</html>`;

	// Storage honesty guard (P5): the plan block must NEVER touch browser
	// storage; any access is recorded and asserted zero.
	await page.addInitScript(() => {
		window.__maiTaskPlanStorage = [];
		for (const kind of ['localStorage', 'sessionStorage'] as const) {
			Object.defineProperty(window, kind, {
				configurable: true,
				get() {
					window.__maiTaskPlanStorage?.push('get ' + kind);
					return undefined;
				},
				set() {
					window.__maiTaskPlanStorage?.push('set ' + kind);
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

async function openFixture(page: Page, scenario: Scenario, pollMs = 0): Promise<void> {
	await page.addInitScript(
		({ scenarioValue, pollValue }) => {
			window.__maiTaskPlanScenario = scenarioValue;
			window.__maiTaskPlanPollMs = pollValue;
		},
		{ scenarioValue: scenario, pollValue: pollMs }
	);
	await page.goto(FIXTURE_URL);
}

// --- rows -------------------------------------------------------------------

test('U1: at 360x640 the ready plan renders bounded dedup pages for items, evidence and the ledger', async ({
	page
}) => {
	await page.setViewportSize({ width: 360, height: 640 });
	await openFixture(page, 'ready');

	const block = page.getByTestId('task-plan');
	await expect(block).toHaveAttribute('data-state', 'ready');

	// Scope/progress: server-backed header values only.
	await expect(page.getByTestId('task-plan-title')).toHaveText(
		'Renew the TLS certificate on edge-01'
	);
	await expect(page.getByTestId('task-plan-progress')).toContainText(
		'phase inspect (advisory) · in progress · rev 4'
	);
	await expect(page.getByTestId('task-plan-progress')).toContainText('steps 2/12 executed');

	// Bounded first pages with honest counts.
	await expect(page.getByTestId('task-plan-step-row')).toHaveCount(10);
	await expect(page.getByTestId('task-plan-steps-meta')).toHaveText('Showing 10 of 12 items.');
	await expect(page.getByTestId('task-plan-evidence-row')).toHaveCount(5);
	await expect(page.getByTestId('task-plan-evidence-meta')).toHaveText('Showing 5 of 5 records.');
	await expect(page.getByTestId('task-plan-revision-row')).toHaveCount(10);
	await expect(page.getByTestId('task-plan-revisions-meta')).toHaveText(
		'Showing 10 of 12 revisions (newest first).'
	);

	// Bounded scroll containers: overflow-y auto + a real scrollbar before Load more.
	const listStats = await page.getByTestId('task-plan-step-list').evaluate((el) => {
		const style = window.getComputedStyle(el);
		return {
			overflowY: style.overflowY,
			maxHeight: style.maxHeight,
			scrollHeight: el.scrollHeight,
			clientHeight: el.clientHeight
		};
	});
	expect(listStats.overflowY).toBe('auto');
	expect(listStats.maxHeight).not.toBe('none');
	expect(listStats.scrollHeight).toBeGreaterThan(listStats.clientHeight);

	// Load more items: the overlapping + within-page-duplicated second page
	// deduplicates to exactly the 12 unique active rows.
	await page.getByTestId('task-plan-steps-more').click();
	await expect(page.getByTestId('task-plan-step-row')).toHaveCount(12);
	await expect(page.getByTestId('task-plan-steps-meta')).toHaveText('Showing 12 of 12 items.');
	await expect(page.getByTestId('task-plan-steps-more')).toHaveCount(0);

	// Dependencies and evidence links render as value-free text.
	const s3 = page.getByTestId('task-plan-step-row').filter({ hasText: 'execute the script' }).first();
	await expect(s3.getByTestId('task-plan-step-deps')).toHaveText('depends on s2');
	await expect(s3).toContainText('mutating');
	const s1 = page.getByTestId('task-plan-step-row').filter({ hasText: 'snapshot current state' }).first();
	await expect(s1.getByTestId('task-plan-step-evidence')).toContainText(
		'message d4e5f6a7…0001'
	);

	// Load more revisions: same bounded + dedup behavior (newest first).
	await page.getByTestId('task-plan-revisions-more').click();
	await expect(page.getByTestId('task-plan-revision-row')).toHaveCount(12);
	await expect(page.getByTestId('task-plan-revisions-more')).toHaveCount(0);

	// U1: no horizontal overflow at 360x640.
	const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
	expect(scrollWidth).toBeLessThanOrEqual(360);
});

test('criterion 2: executed and verified are visually distinct — never one shared done badge', async ({
	page
}) => {
	await openFixture(page, 'ready');

	// s1: executed AND verified — two DIFFERENT badges, two different words.
	const s1 = page
		.getByTestId('task-plan-step-row')
		.filter({ hasText: 'snapshot current state' })
		.first();
	await expect(s1.getByTestId('task-plan-step-status')).toHaveAttribute('data-state', 'executed');
	await expect(s1.getByTestId('task-plan-step-status')).toHaveText('executed');
	await expect(s1.getByTestId('task-plan-step-verification')).toHaveAttribute('data-state', 'passed');
	await expect(s1.getByTestId('task-plan-step-verification')).toHaveText('verified');

	// s3: executed but NOT verified — execution alone never reads as done.
	const s3 = page
		.getByTestId('task-plan-step-row')
		.filter({ hasText: 'execute the script' })
		.first();
	await expect(s3.getByTestId('task-plan-step-status')).toHaveAttribute('data-state', 'executed');
	await expect(s3.getByTestId('task-plan-step-verification')).toHaveAttribute(
		'data-state',
		'not_checked'
	);
	await expect(s3.getByTestId('task-plan-step-verification')).toHaveText('not checked');

	// Visual distinctness is real style, not just class names — and the
	// text labels carry the meaning (never color alone).
	const badgeStyles = await page.evaluate(() => {
		const executed = document.querySelector('[data-testid="task-plan-step-status"][data-state="executed"]');
		const verified = document.querySelector(
			'[data-testid="task-plan-step-verification"][data-state="passed"]'
		);
		const executedStyle = executed === null ? null : window.getComputedStyle(executed);
		const verifiedStyle = verified === null ? null : window.getComputedStyle(verified);
		return {
			executedClass: executed?.className ?? '',
			verifiedClass: verified?.className ?? '',
			executedBackground: executedStyle?.backgroundColor ?? '',
			verifiedBackground: verifiedStyle?.backgroundColor ?? '',
			executedFont: executedStyle?.fontWeight ?? '',
			verifiedFont: verifiedStyle?.fontWeight ?? ''
		};
	});
	expect(badgeStyles.executedClass).not.toBe(badgeStyles.verifiedClass);
	expect(badgeStyles.executedBackground).not.toBe(badgeStyles.verifiedBackground);
	expect(badgeStyles.executedBackground).not.toBe('rgba(0, 0, 0, 0)');
	expect(badgeStyles.verifiedBackground).not.toBe('rgba(0, 0, 0, 0)');

	// No fabricated "done" vocabulary anywhere in the block.
	const blockText = await page.getByTestId('task-plan').innerText();
	expect(blockText).not.toContain('done');
});

test('criterion 1/2: removing a RUNNING item proposes a supersede and reports the honest disposition', async ({
	page
}) => {
	await openFixture(page, 'ready');

	await page.getByTestId('task-plan-proposal-op').selectOption('supersede');
	await page.getByTestId('task-plan-proposal-target').selectOption('s2');

	// The running-item honesty warning is visible BEFORE anything is sent.
	await expect(page.getByTestId('task-plan-running-warning')).toContainText(
		'Removal is a proposal only — it does NOT stop any running process'
	);
	await expect(page.getByTestId('task-plan-running-warning')).toContainText(
		'the run reports its real state'
	);

	await page.getByTestId('task-plan-proposal-submit').click();
	await expect(page.getByTestId('task-plan-proposal-outcome')).toHaveAttribute(
		'data-state',
		'applied'
	);
	await expect(page.getByTestId('task-plan-proposal-outcome')).toContainText(
		'applied as revision 5'
	);
	await expect(page.getByTestId('task-plan-proposal-outcome')).toContainText(
		'previous rows stay recorded'
	);

	// The wire payload is a bounded SUPERSEDE proposal under CAS: history is
	// appended, never rewritten.
	const submissions = await callsOf(page, 'submitTaskCorrection');
	expect(submissions).toHaveLength(1);
	const submitted = submissions[0].payload as SubmittedCorrection;
	expect(submitted.patch.base_revision).toBe(4);
	expect(submitted.patch.source_message_id).toBe(ANCHOR);
	const operation = submitted.patch.operations[0];
	expect(operation.op).toBe('supersede');
	expect(operation.supersedes_step_id).toBe('s2');
	const replacement = operation.step as Record<string, unknown>;
	expect(replacement.status).toBe('skipped');
	expect(replacement.skip_reason).toContain('removed by user revision proposal (was in progress)');

	// The active list re-rendered from the server: s2 replaced by its
	// skipped row at the SAME stable slot; the ledger recorded the edit.
	await expect(page.getByTestId('task-plan-step-row')).toHaveCount(10);
	const replaced = page
		.getByTestId('task-plan-step-row')
		.filter({ hasText: 'apply the change' })
		.first();
	await expect(replaced).toHaveAttribute('data-step-status', 'skipped');
	await expect(replaced).toContainText('skip reason: removed by user revision proposal');
	// Exactly ONE 'apply the change' row remains: the superseded in-progress
	// row left the active list (and stays only in the recorded history).
	await expect(
		page.getByTestId('task-plan-step-row').filter({ hasText: 'apply the change' })
	).toHaveCount(1);
	const ledgerRow = page.getByTestId('task-plan-revision-row').first();
	await expect(ledgerRow).toContainText('rev 5 · plan_patch');
	await expect(ledgerRow).toContainText('by user');
	await expect(ledgerRow).toContainText('item s2');
	await expect(ledgerRow).toContainText(`source message ${ANCHOR}`);
});

test('criterion 1: a stale revision surfaces the structured 409 conflict, overwrites nothing, and re-applies after refresh', async ({
	page
}) => {
	await openFixture(page, 'ready');

	// The server moves on (a tool/metadata update) while the form is open.
	await page.evaluate(() => window.__maiTaskPlanApi?.bumpRevision(5));

	await page.getByTestId('task-plan-proposal-op').selectOption('add');
	await page.getByTestId('task-plan-proposal-title').fill('Verify service health after restart');
	await page.getByTestId('task-plan-proposal-submit').click();

	// The honest conflict state — not a fake success, not a silent overwrite.
	await expect(page.getByTestId('task-plan-proposal-outcome')).toHaveAttribute(
		'data-state',
		'conflict'
	);
	await expect(page.getByTestId('task-plan-proposal-outcome')).toContainText('Revision conflict');
	await expect(page.getByTestId('task-plan-proposal-outcome')).toContainText('revision_conflict');
	await expect(page.getByTestId('task-plan-proposal-outcome')).toContainText(
		'Nothing was overwritten'
	);

	// Nothing was applied: same 10 visible rows, s2 untouched, ledger total
	// unchanged at 12 and the header still shows the STALE rev 4.
	await expect(page.getByTestId('task-plan-step-row')).toHaveCount(10);
	await expect(
		page.getByTestId('task-plan-step-row').filter({ hasText: 'apply the change' })
	).toHaveCount(1);
	await expect(page.getByTestId('task-plan-revisions-meta')).toHaveText(
		'Showing 10 of 12 revisions (newest first).'
	);
	await expect(page.getByTestId('task-plan-progress')).toContainText('rev 4 ·');

	// Reconnect: the bounded refresh adopts the server revision…
	await page.getByTestId('task-plan-refresh').click();
	await expect(page.getByTestId('task-plan-progress')).toContainText('rev 5 ·');

	// …and the SAME edit re-applies under the fresh CAS base.
	await page.getByTestId('task-plan-proposal-submit').click();
	await expect(page.getByTestId('task-plan-proposal-outcome')).toHaveAttribute(
		'data-state',
		'applied'
	);
	await expect(page.getByTestId('task-plan-proposal-outcome')).toContainText(
		'applied as revision 6'
	);
	const submissions = await callsOf(page, 'submitTaskCorrection');
	expect(submissions).toHaveLength(2);
	const bases = submissions.map(
		(call) => (call.payload as SubmittedCorrection).patch.base_revision
	);
	expect(bases).toEqual([4, 5]);
	await expect(page.getByTestId('task-plan-steps-meta')).toHaveText('Showing 10 of 13 items.');
});

test('criterion 1: the proposal lifecycle distinguishes pending from applied', async ({ page }) => {
	await openFixture(page, 'ready');

	await page.evaluate(() => window.__maiTaskPlanApi?.holdSubmit(true));
	await page.getByTestId('task-plan-proposal-op').selectOption('add');
	await page.getByTestId('task-plan-proposal-title').fill('Schedule the maintenance window');
	await page.getByTestId('task-plan-proposal-submit').click();

	await expect(page.getByTestId('task-plan-proposal-outcome')).toHaveAttribute(
		'data-state',
		'pending'
	);
	await expect(page.getByTestId('task-plan-proposal-outcome')).toContainText(
		'Proposal pending — awaiting the server’s revision acknowledgement'
	);
	await expect(page.getByTestId('task-plan-proposal-submit')).toBeDisabled();

	await page.evaluate(() => window.__maiTaskPlanApi?.holdSubmit(false));
	await expect(page.getByTestId('task-plan-proposal-outcome')).toHaveAttribute(
		'data-state',
		'applied'
	);
	await expect(page.getByTestId('task-plan-proposal-outcome')).toContainText(
		'applied as revision 5'
	);
	await expect(page.getByTestId('task-plan-proposal-submit')).toBeEnabled();
});

test('U2: an empty task renders every explicit gap and invents nothing', async ({ page }) => {
	await openFixture(page, 'empty');

	const block = page.getByTestId('task-plan');
	await expect(block).toHaveAttribute('data-state', 'ready');
	await expect(page.getByTestId('task-plan-steps-empty')).toHaveText('No plan items recorded.');
	await expect(page.getByTestId('task-plan-evidence-empty')).toHaveText(
		'No evidence records linked.'
	);
	// The ONE honest ledger row a created-but-empty task has: its creation.
	await expect(page.getByTestId('task-plan-revision-row')).toHaveCount(1);
	await expect(page.getByTestId('task-plan-revision-row').first()).toContainText(
		'rev 1 · create'
	);
	await expect(page.getByTestId('task-plan-revisions-meta')).toHaveText(
		'Showing 1 of 1 revisions (newest first).'
	);
	await expect(page.getByTestId('task-plan-progress')).toContainText('steps 0/0 executed');
	await expect(page.getByTestId('task-plan-steps-more')).toHaveCount(0);
	await expect(page.getByTestId('task-plan-evidence-more')).toHaveCount(0);
	await expect(page.getByTestId('task-plan-revisions-more')).toHaveCount(0);
});

test('U2: the scope-blind 404 renders the plan unavailable, never fabricated scope', async ({
	page
}) => {
	await openFixture(page, 'absent');

	const block = page.getByTestId('task-plan');
	await expect(block).toHaveAttribute('data-state', 'unavailable');
	await expect(page.getByTestId('task-plan-unavailable')).toContainText('Task plan unavailable.');
	await expect(page.getByTestId('task-plan-scope')).toHaveCount(0);
	await expect(page.getByTestId('task-plan-step-row')).toHaveCount(0);
});

test('U2: a transport failure renders the real code and no invented data', async ({ page }) => {
	await openFixture(page, 'error');

	const block = page.getByTestId('task-plan');
	await expect(block).toHaveAttribute('data-state', 'error');
	await expect(page.getByTestId('task-plan-error')).toContainText('Task plan could not be loaded.');
	await expect(page.getByTestId('task-plan-error-code')).toHaveText('unavailable');
	await expect(page.getByTestId('task-plan-scope')).toHaveCount(0);
});

test('fail-closed: an unbound services bag renders the explicit state with zero fetches', async ({
	page
}) => {
	await openFixture(page, 'unbound');

	const block = page.getByTestId('task-plan');
	await expect(block).toHaveAttribute('data-state', 'unbound');
	await expect(page.getByTestId('task-plan-unbound')).toContainText(
		'No task services are bound; nothing is fetched or fabricated.'
	);
	expect(await callsOf(page, 'getConversationTask')).toHaveLength(0);
	expect(await callsOf(page, 'listTaskSteps')).toHaveLength(0);
});

test('honest form: without a source-message anchor proposals are disabled, not fabricated', async ({
	page
}) => {
	await openFixture(page, 'unanchored');

	const block = page.getByTestId('task-plan');
	await expect(block).toHaveAttribute('data-state', 'ready');
	await expect(page.getByTestId('task-plan-proposal-unanchored')).toContainText(
		'Proposals anchor to a recorded user message'
	);
	await expect(page.getByTestId('task-plan-proposal-unanchored')).toContainText(
		'proposing is disabled'
	);
	await expect(page.getByTestId('task-plan-proposal-submit')).toHaveCount(0);
	expect(await callsOf(page, 'submitTaskCorrection')).toHaveLength(0);
});

test('P5: a task-tool update made while disconnected arrives via the bounded poll with stable item IDs', async ({
	page
}) => {
	await openFixture(page, 'ready', 200);

	await expect(page.getByTestId('task-plan')).toHaveAttribute('data-state', 'ready');
	await expect(page.getByTestId('task-plan-step-row')).toHaveCount(10);

	const s5 = page.getByTestId('task-plan-step-row').nth(4);
	await expect(s5).toHaveAttribute('data-step-id', 's5');
	await expect(s5).toHaveAttribute('data-step-status', 'pending');

	// The executor completes s5 server-side; the UI discovers it ONLY
	// through the shared server revision — same row identity, no duplicates.
	await page.evaluate(() => window.__maiTaskPlanApi?.executorCompleteStep('s5'));
	await expect(s5).toHaveAttribute('data-step-status', 'executed', { timeout: 10000 });
	await expect(s5).toHaveAttribute('data-step-verification', 'passed');
	await expect(page.getByTestId('task-plan-progress')).toContainText('rev 5 ·');
	await expect(page.getByTestId('task-plan-steps-meta')).toHaveText('Showing 10 of 12 items.');
	await expect(page.getByTestId('task-plan-step-row')).toHaveCount(10);
});

test('P5: an episode created after the mount promotes the section via the quiet probe', async ({
	page
}) => {
	await openFixture(page, 'absent', 200);

	const block = page.getByTestId('task-plan');
	await expect(block).toHaveAttribute('data-state', 'unavailable');

	// The first turn creates the episode server-side; no reopen needed.
	await page.evaluate(() => window.__maiTaskPlanApi?.seedEpisode());
	await expect(block).toHaveAttribute('data-state', 'ready', { timeout: 10000 });
	await expect(page.getByTestId('task-plan-title')).toHaveText(
		'Renew the TLS certificate on edge-01'
	);
	await expect(page.getByTestId('task-plan-step-row')).toHaveCount(3);
});

test('criterion 1: remount (reopen) re-derives the same plan read-only with zero storage access', async ({
	page
}) => {
	await openFixture(page, 'ready');

	const block = page.getByTestId('task-plan');
	await expect(block).toHaveAttribute('data-state', 'ready');
	await expect(page.getByTestId('task-plan-step-row')).toHaveCount(10);

	// Simulated reopen: destroy and recreate the block; the server re-derives
	// the SAME plan (no canonical browser persistence).
	await page.evaluate(() => window.__maiTaskPlanApi?.remount());
	await expect
		.poll(async () => (await callsOf(page, 'getConversationTask')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(2);
	await expect(block).toHaveAttribute('data-state', 'ready');
	await expect(page.getByTestId('task-plan-title')).toHaveText(
		'Renew the TLS certificate on edge-01'
	);
	await expect(page.getByTestId('task-plan-steps-meta')).toHaveText('Showing 10 of 12 items.');

	// Reads only: the mutation surface was never touched on a pure reopen.
	const ops = await page.evaluate(() => (window.__maiTaskPlanApi?.calls ?? []).map((c) => c.op));
	for (const op of ops) {
		expect([
			'getConversationTask',
			'listTaskSteps',
			'listTaskEvidence',
			'listTaskRevisions'
		]).toContain(op);
	}
	expect(await callsOf(page, 'submitTaskCorrection')).toHaveLength(0);

	// Storage honesty: zero localStorage/sessionStorage access, ever.
	const storageAccess = await page.evaluate(() => window.__maiTaskPlanStorage ?? []);
	expect(storageAccess).toEqual([]);
});
