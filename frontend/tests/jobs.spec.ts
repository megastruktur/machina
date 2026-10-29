import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-05-web-jobs — real-browser proofs for the jobs & schedules panel.
 *
 * The harness compiles the REAL feature panel (`JobsPanel.svelte`), the REAL
 * feature entry (`05-web-jobs.ts`), the REAL panel host/registry and the REAL
 * SSE client primitive into a throwaway fixture app served from virtual
 * modules — nothing hits disk and no real backend exists. The fixture API
 * mirrors the reviewed producers 1:1 for the seven REST methods; the event
 * stream is the REAL `browserRunEventStream` binding: the panel's fetch goes
 * through `subscribeEvents` and is answered by Playwright route handlers, so
 * the concrete producer URL, the `Last-Event-ID` cursor carry, frame parsing
 * and reconnect behavior are exercised on the wire, not stubbed (EVT-REPLAY
 * consumer half; contracts C05 rule 4, C08 rule 7).
 */

const FIXTURE_URL = '/mai-jobs-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-jobs-app.svelte';
const ENTRY_ID = 'virtual:mai-jobs-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/JobsPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/05-web-jobs.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const RUN_A = '1a1a1a1a-1111-4222-8333-444455556666';
const RUN_C = '3c3c3c3c-1111-4222-8333-444455556666';
const RUN_D = '4d4d4d4d-1111-4222-8333-444455556666';
const SCHED_1 = '5a5a5a5a-1111-4222-8333-444455556666';
const WINDOW_A = '6a6a6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9c';
const WINDOW_B = '7b7b6a7b-8c9d-4e8a-8a9b-4c5d6e7f8a9d';
const WINDOW_GLOBAL = '3c3c3c3c-9999-4222-8333-444455556666';
const WINDOW_BARE = '4d4d4d4d-9999-4222-8333-444455556666';
const WINDOW_SIBLING = '5e5e5e5e-9999-4222-8333-444455556666';

type Fail = { status: number; code: string; message: string };

interface JobsWindowApi {
	calls: Array<{ op: string; payload?: unknown; arg?: unknown }>;
	armRunScript(runId: string, snapshots: unknown[]): void;
	setRunState(runId: string, state: string, extra?: Record<string, unknown>): void;
	failNextListRuns(failure: Fail | null): void;
	failNextListSchedules(failure: Fail | null): void;
	failNextCreateSchedule(failure: Fail | null): void;
	failNextReadRun(failure: Fail | null): void;
	failNextCancel(failure: Fail | null): void;
	failNextUpdateSchedule(failure: Fail | null): void;
	failNextDeleteSchedule(failure: Fail | null): void;
	armCancelOutcome(outcome: string | null): void;
	toggleHost(host: 'A' | 'B'): void;
	addRuns(serverId: string, count: number, prefix: string): void;
	registry: {
		kinds: string[];
		scopeChecks: { serverScope: string; globalScope: string; unknownKind: string };
		defaultGeometry: unknown;
		minGeometry: unknown;
		instanceKey: string | null;
	};
}

declare global {
	interface Window {
		__maiJobsApi?: JobsWindowApi;
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
 * The fixture app. Two server-scoped jobs hosts (A: populated target; B:
 * empty target), one bare host (no services), one global-scope host (registry
 * scope rejection) and one benign sibling panel.
 */
const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry, definePanelEntry, singletonInstanceKey } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { ApiError } from '${CLIENT}';
	import {
		jobsEntry,
		jobsServices,
		browserRunEventStream,
		JOBS_PANEL_KIND
	} from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const RUN_A = '${RUN_A}';
	const RUN_C = '${RUN_C}';
	const RUN_D = '${RUN_D}';
	const SCHED_1 = '${SCHED_1}';

	// --- fixture backend: replayed durable rows (the facade never guesses) ----

	function step(seq, state) {
		return { seq, capability_id: 'host.shell', state, receipt: null };
	}
	function runRow(runId, serverId, state, steps, overrides) {
		return Object.assign(
			{
				run_id: runId,
				server_id: serverId,
				parent_run_id: null,
				depth: 0,
				profile_revision: 4,
				prompt_revision: 2,
				model_profile: 'direct',
				requested_grants: ['host.shell'],
				label: 'Deploy API',
				state,
				cancel_requested_at: null,
				submitted_at: '2026-10-15T09:00:00Z',
				updated_at: '2026-10-15T09:01:00Z',
				steps
			},
			overrides ?? {}
		);
	}
	const runs = {
		[SERVER_A]: {
			[RUN_A]: runRow(RUN_A, SERVER_A, 'running', [step(1, 'succeeded'), step(2, 'running')], {
				label: 'Collect telemetry'
			}),
			[RUN_C]: runRow(RUN_C, SERVER_A, 'succeeded', [step(1, 'succeeded')], {
				label: 'Rotate credentials'
			}),
			[RUN_D]: runRow(RUN_D, SERVER_A, 'queued', [], { label: 'Probe fleet' })
		},
		[SERVER_B]: {}
	};
	const runScripts = {};

	function scheduleRow(scheduleId, serverId, cron, timezone, handler, enabled) {
		return {
			schedule_id: scheduleId,
			server_id: serverId,
			cron,
			cron_timezone: timezone,
			misfire_policy: 'skip_missed',
			enabled,
			run_spec: { handler_name: handler, payload: { window: 7 } },
			created_at: '2026-10-15T08:00:00Z',
			updated_at: '2026-10-15T08:00:00Z'
		};
	}
	const schedules = {
		[SERVER_A]: [scheduleRow(SCHED_1, SERVER_A, '0 * * * *', 'UTC', 'collect.logs', true)],
		[SERVER_B]: []
	};

	const calls = [];
	let failNextListRuns = null;
	let failNextListSchedules = null;
	let failNextCreateSchedule = null;
	let failNextReadRun = null;
	let failNextCancel = null;
	let failNextUpdateSchedule = null;
	let failNextDeleteSchedule = null;
	let cancelOutcomeArmed = null;

	function findRun(runId) {
		for (const serverRows of Object.values(runs)) {
			if (serverRows[runId]) return serverRows[runId];
		}
		return null;
	}
	function findSchedule(scheduleId) {
		for (const rows of Object.values(schedules)) {
			const row = rows.find((candidate) => candidate.schedule_id === scheduleId);
			if (row) return row;
		}
		return null;
	}
	function runViewOf(row) {
		const view = Object.assign({}, row);
		delete view.steps;
		return view;
	}

	const restApi = {
		// W3 narrow name seam: resolve the pinned scope's enrolled name; an
		// unknown/unresolvable id 404s so the panel's nonfatal fallback is
		// exercisable (host B pins such a target).
		async readServerName(serverId) {
			calls.push({ op: 'readServerName', arg: serverId });
			if (serverId === SERVER_A) return 'Alpha Build Host';
			throw new ApiError(404, 'not_found', 'no canonical profile for this server id');
		},
		async listRuns(serverId, page) {
			calls.push({ op: 'listRuns', arg: serverId, payload: JSON.parse(JSON.stringify(page ?? {})) });
			if (failNextListRuns) {
				const failure = failNextListRuns;
				failNextListRuns = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			// The W1 contract: filters apply BEFORE pagination — the same
			// predicate drives the slice AND the pre-slice total.
			let rows = Object.values(runs[serverId] ?? {}).map(runViewOf);
			if (page?.q !== undefined && page.q !== '') {
				const needle = page.q.toLowerCase();
				rows = rows.filter(
					(row) =>
						row.run_id.toLowerCase().includes(needle) ||
						String(row.label ?? '').toLowerCase().includes(needle) ||
						String(row.model_profile ?? '').toLowerCase().includes(needle)
				);
			}
			if (page?.date_from !== undefined && page.date_from !== '') {
				rows = rows.filter((row) => row.submitted_at >= page.date_from);
			}
			if (page?.date_to !== undefined && page.date_to !== '') {
				rows = rows.filter((row) => row.submitted_at <= page.date_to);
			}
			const total = rows.length;
			const limit = page?.limit ?? 50;
			const offset = page?.offset ?? 0;
			return {
				runs: rows.slice(offset, offset + limit),
				limit,
				offset,
				total
			};
		},
		async readRun(runId) {
			calls.push({ op: 'readRun', arg: runId });
			if (failNextReadRun) {
				const failure = failNextReadRun;
				failNextReadRun = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const row = findRun(runId);
			if (!row) throw new ApiError(404, 'not_found', 'no run stored for this id');
			const script = runScripts[runId];
			if (script && script.length > 0) {
				// The facade replays persisted fields of the SAME durable row; a
				// scripted snapshot never replaces the run's identity binding.
				Object.assign(row, script.shift());
			}
			return JSON.parse(JSON.stringify(row));
		},
		async cancelRun(runId, payload) {
			calls.push({ op: 'cancelRun', arg: runId, payload: JSON.parse(JSON.stringify(payload)) });
			if (failNextCancel) {
				const failure = failNextCancel;
				failNextCancel = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const row = findRun(runId);
			if (!row) throw new ApiError(404, 'not_found', 'no run stored for this id');
			if (cancelOutcomeArmed !== null) {
				// The engine reported a KNOWN terminal outcome within the budget.
				row.state = cancelOutcomeArmed;
				row.updated_at = '2026-10-15T09:06:00Z';
				return {
					run_id: runId,
					state: row.state,
					outcome: row.state,
					requested_at: '2026-10-15T09:05:00Z',
					outcome_at: '2026-10-15T09:06:00Z',
					requested_now: true,
					unresolved_seqs: []
				};
			}
			// Real boundary behavior (jobs.py): the request is recorded ONCE and
			// the observation budget expires into an honest pending — 'outcome'
			// null never claims "cancelled" (engine guard rule 3).
			row.cancel_requested_at = '2026-10-15T09:05:00Z';
			return {
				run_id: runId,
				state: row.state,
				outcome: null,
				requested_at: '2026-10-15T09:05:00Z',
				outcome_at: null,
				requested_now: true,
				unresolved_seqs: []
			};
		},
		async listSchedules(serverId, page) {
			calls.push({ op: 'listSchedules', arg: serverId, payload: JSON.parse(JSON.stringify(page ?? {})) });
			if (failNextListSchedules) {
				const failure = failNextListSchedules;
				failNextListSchedules = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const rows = schedules[serverId] ?? [];
			const limit = page?.limit ?? 50;
			const offset = page?.offset ?? 0;
			return {
				schedules: JSON.parse(JSON.stringify(rows.slice(offset, offset + limit))),
				limit,
				offset,
				total: rows.length
			};
		},
		async createSchedule(payload) {
			calls.push({ op: 'createSchedule', payload: JSON.parse(JSON.stringify(payload)) });
			if (failNextCreateSchedule) {
				const failure = failNextCreateSchedule;
				failNextCreateSchedule = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			if (!(runs[payload.server_id] || schedules[payload.server_id])) {
				throw new ApiError(404, 'not_found', 'no canonical profile for this server id');
			}
			const row = {
				schedule_id: crypto.randomUUID(),
				server_id: payload.server_id,
				cron: payload.cron,
				cron_timezone: payload.cron_timezone ?? 'UTC',
				misfire_policy: 'skip_missed',
				enabled: true,
				run_spec: JSON.parse(JSON.stringify(payload.run_spec)),
				created_at: '2026-10-15T09:10:00Z',
				updated_at: '2026-10-15T09:10:00Z'
			};
			schedules[payload.server_id].push(row);
			return JSON.parse(JSON.stringify(row));
		},
		async updateSchedule(scheduleId, payload) {
			calls.push({ op: 'updateSchedule', arg: scheduleId, payload: JSON.parse(JSON.stringify(payload)) });
			if (failNextUpdateSchedule) {
				const failure = failNextUpdateSchedule;
				failNextUpdateSchedule = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const row = findSchedule(scheduleId);
			if (!row) throw new ApiError(404, 'not_found', 'no schedule stored for this id');
			row.enabled = payload.enabled;
			row.updated_at = '2026-10-15T09:11:00Z';
			return JSON.parse(JSON.stringify(row));
		},
		async deleteSchedule(scheduleId) {
			calls.push({ op: 'deleteSchedule', arg: scheduleId });
			if (failNextDeleteSchedule) {
				const failure = failNextDeleteSchedule;
				failNextDeleteSchedule = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			for (const rows of Object.values(schedules)) {
				const index = rows.findIndex((candidate) => candidate.schedule_id === scheduleId);
				if (index !== -1) {
					rows.splice(index, 1);
					return;
				}
			}
			throw new ApiError(404, 'not_found', 'no schedule stored for this id');
		}
	};

	// The ONE real wire path: the stream goes through the feature entry's
	// browser binding and the reviewed client primitive, so the SSE URL, the
	// Last-Event-ID carry and frame parsing are exercised on the wire.
	const api = Object.assign({}, restApi, { streamRunEvents: browserRunEventStream });
	const services = jobsServices(api);

	// --- registry: the REAL feature entry + one benign sibling ----------------
	const sibling = createRawSnippet(() => {
		return {
			render: () =>
				'<div class="mai-sibling" data-testid="sibling-panel">' +
				'<p>Sibling fixture panel — benign, no domain data.</p>' +
				'</div>',
			setup: () => {}
		};
	});
	const registryResult = createPanelRegistry([
		jobsEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;
	const entryCheck = definePanelEntry(jobsEntry());
	if (!entryCheck.ok) throw new Error('feature entry rejected: ' + entryCheck.issue.code);

	// --- hosts ----------------------------------------------------------------
	let showA = $state(true);

	function descriptor(windowId, scope, z, kind) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: kind ?? JOBS_PANEL_KIND,
			scope,
			geometry: { x: 8, y: 8, width: 720, height: 560 },
			presentation: 'normal',
			z,
			title_override: null
		};
	}

	const windowApi = {
		calls,
		armRunScript(runId, snapshots) {
			runScripts[runId] = structuredClone(snapshots);
		},
		setRunState(runId, state, extra) {
			const row = findRun(runId);
			if (row) Object.assign(row, { state }, extra ?? {});
		},
		failNextListRuns(failure) {
			failNextListRuns = failure;
		},
		failNextListSchedules(failure) {
			failNextListSchedules = failure;
		},
		failNextCreateSchedule(failure) {
			failNextCreateSchedule = failure;
		},
		failNextReadRun(failure) {
			failNextReadRun = failure;
		},
		failNextCancel(failure) {
			failNextCancel = failure;
		},
		failNextUpdateSchedule(failure) {
			failNextUpdateSchedule = failure;
		},
		failNextDeleteSchedule(failure) {
			failNextDeleteSchedule = failure;
		},
		armCancelOutcome(outcome) {
			cancelOutcomeArmed = outcome;
		},
		toggleHost(host) {
			if (host === 'A') showA = !showA;
		},
		// W3 pagination fixture: distinct submitted_at for deterministic
		// ordering, unique run ids and useful labels.
		addRuns(serverId, count, prefix) {
			const store = runs[serverId];
			if (!store) return;
			for (let index = 0; index < count; index += 1) {
				const runId = crypto.randomUUID();
				store[runId] = runRow(runId, serverId, 'succeeded', [], {
					label: prefix + ' ' + String(index + 1).padStart(2, '0'),
					submitted_at: '2026-10-15T10:' + String(10 + index).padStart(2, '0') + ':00Z',
					updated_at: '2026-10-15T10:' + String(10 + index).padStart(2, '0') + ':30Z'
				});
			}
		},
		registry: (() => {
			function checkCode(kind, scope) {
				const result = registry.checkScope(kind, scope);
				return result.ok ? 'ok' : result.issue.code;
			}
			return {
				kinds: Array.from(registry.knownPanelKinds()).sort(),
				scopeChecks: {
					serverScope: checkCode(JOBS_PANEL_KIND, { kind: 'server', server_id: SERVER_A }),
					globalScope: checkCode(JOBS_PANEL_KIND, { kind: 'global' }),
					unknownKind: checkCode('not-a-panel', { kind: 'server', server_id: SERVER_A })
				},
				defaultGeometry: registry.defaultGeometry(JOBS_PANEL_KIND),
				minGeometry: registry.minGeometry(JOBS_PANEL_KIND),
				instanceKey: singletonInstanceKey(JOBS_PANEL_KIND, entryCheck.value, {
					kind: 'server',
					server_id: SERVER_A
				})
			};
		})()
	};
	window.__maiJobsApi = windowApi;
</script>

<section data-testid="host-a" aria-label="Host A">
	<ComponentHost label="Host A">
		{#if showA}
			<PanelHost
				onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
				{registry}
				descriptor={descriptor('${WINDOW_A}', { kind: 'server', server_id: SERVER_A }, 1)}
				{services}
			/>
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-b" aria-label="Host B">
	<ComponentHost label="Host B">
		<PanelHost
			onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
			{registry}
			descriptor={descriptor('${WINDOW_B}', { kind: 'server', server_id: SERVER_B }, 2)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-sibling" aria-label="Host sibling">
	<ComponentHost label="Host sibling">
		<PanelHost
			onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
			{registry}
			descriptor={descriptor('${WINDOW_SIBLING}', { kind: 'server', server_id: SERVER_B }, 3, 'fixture-notes')}
		/>
	</ComponentHost>
</section>
<section data-testid="host-global" aria-label="Host global">
	<ComponentHost label="Host global">
		<PanelHost
			onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
			{registry}
			descriptor={descriptor('${WINDOW_GLOBAL}', { kind: 'global' }, 4)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-bare" aria-label="Host bare">
	<ComponentHost label="Host bare">
		<PanelHost
			onpanelerror={(windowId, panelKind, error) => console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error)}
			{registry}
			descriptor={descriptor('${WINDOW_BARE}', { kind: 'server', server_id: SERVER_A }, 5)}
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
// Event-stream wire control (real fetch, answered by route handlers)
// ---------------------------------------------------------------------------

interface StreamFrame {
	seq: number;
	type: string;
	/** Optional frozen envelope `data` (agent rounds, tool results, failures). */
	data?: unknown;
}

interface StreamSession {
	runId: string;
	/** Frames to deliver before the stream closes; empty for abort-only. */
	frames?: StreamFrame[];
	/** Reject the connection (API restart / network failure) instead of closing. */
	abort?: boolean;
}

let streamSessions: StreamSession[] = [];
let streamRequests: Array<{ runId: string; cursor: string | null; path: string }> = [];
/** Requests held open because no session was queued yet (the loop pauses). */
let heldStreamRoutes: Route[] = [];

function sseBody(runId: string, frames: StreamFrame[]): string {
	return frames
		.map(
			(frame) =>
				`id: ${frame.seq}\n` +
				`data: ${JSON.stringify({
					seq: frame.seq,
					run_id: runId,
					ts: '2026-10-15T10:00:00Z',
					type: frame.type,
					v: 1,
					data: frame.data ?? {}
				})}\n\n`
		)
		.join('');
}

function fulfillStream(route: Route, session: StreamSession): Promise<void> {
	return route.fulfill({
		status: 200,
		contentType: 'text/event-stream',
		headers: { 'cache-control': 'no-store' },
		body: sseBody(session.runId, session.frames ?? [])
	});
}

/**
 * One SSE request: recorded (run id, cursor, path), then either answered from
 * the queued session or held open — a held request blocks the panel's await,
 * which keeps the reconnect loop deterministic between scripted sessions.
 */
async function handleEventStream(route: Route): Promise<void> {
	const url = new URL(route.request().url());
	const match = url.pathname.match(/^\/api\/v1\/jobs\/runs\/([^/]+)\/events$/);
	if (!match) {
		await route.abort();
		return;
	}
	streamRequests.push({
		runId: match[1],
		cursor: route.request().headers()['last-event-id'] ?? null,
		path: url.pathname
	});
	const session = streamSessions.shift();
	if (session) {
		if (session.abort) await route.abort();
		else await fulfillStream(route, session);
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
	void (next.abort ? route.abort() : fulfillStream(route, next)).catch(() => {
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
		name: 'mai-jobs-harness',
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
			minify: false,
			outDir: 'node_modules/.mai-jobs-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'jobs-[hash].js',
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
	streamSessions = [];
	streamRequests = [];
	heldStreamRoutes = [];
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI jobs fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script>
</body>
</html>`;

	// The catch-all fixture route is registered FIRST; the SSE route below is
	// registered SECOND and therefore wins for the event-stream URLs.
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
	await page.route('**/api/v1/jobs/runs/*/events', (route) => handleEventStream(route));
	// Surface runtime failures in the receipt: an uncaught page error is a
	// harness/panel defect, never a silent pass.
	page.on('pageerror', (error) => {
		console.log(`[pageerror] ${error.stack ?? error.message}`);
	});
	page.on('console', (message) => {
		if (message.type() === 'error') console.log(`[console.error] ${message.text()}`);
	});
	await page.goto(FIXTURE_URL);
});

/**
 * Fixture-API boundary. Playwright serializes the evaluated function source
 * (closures over test scope do not transfer), so every helper below is
 * self-contained and passes only serializable primitive/array arguments.
 */

/** Call one fixture-api method inside the page; args must be serializable. */
async function apiCall(page: Page, method: string, args: unknown[] = []): Promise<void> {
	const pair: [string, unknown[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiJobsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		// Dynamic dispatch by serialized name: the fixture's own typed API
		// viewed as a name→method table (a static per-method switch would
		// duplicate every signature); the typeof guard below still checks
		// the lookup before any call.
		const table = value as unknown as Record<string, (...a: never[]) => void>;
		const fn = table[name];
		if (typeof fn !== 'function') throw new Error(`unknown fixture method ${name}`);
		fn.apply(value, callArgs as never[]);
	}, pair);
}

interface RecordedCall {
	op: string;
	payload?: unknown;
	arg?: unknown;
}

interface RegistrySummary {
	kinds: string[];
	scopeChecks: { serverScope: string; globalScope: string; unknownKind: string };
	defaultGeometry: unknown;
	minGeometry: unknown;
	instanceKey: string | null;
}

/** Snapshot the fixture's recorded calls and precomputed registry summary. */
async function apiState(
	page: Page
): Promise<{ calls: RecordedCall[]; registry: RegistrySummary }> {
	return page.evaluate(() => {
		const value = window.__maiJobsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		return { calls: value.calls, registry: value.registry };
	});
}

/**
 * Test-only recorded-wire boundary: the fixture recorded these payloads
 * itself (structuredClone of the exact bodies the panel sent).
 */
function recorded<T>(value: unknown, what: string): T {
	if (typeof value !== 'object' || value === null) {
		throw new Error(`${what} was not recorded as an object`);
	}
	return value as T;
}

/** Count of one recorded op (mutation-call assertions compare this). */
async function callsOf(page: Page, op: string): Promise<RecordedCall[]> {
	const { calls } = await apiState(page);
	return calls.filter((call) => call.op === op);
}

const hostA = 'section[data-testid="host-a"]';
const hostB = 'section[data-testid="host-b"]';

test('explicit scope renders honest list states; the entry registers and scopes fail closed', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
	await expect(root.getByTestId('jobs-scope')).toHaveText('Server 8c1d2e3f');
	// (The loading state is a microtask-transient for an in-memory fixture;
	// the loading/error/ready triad is proven by the remount flows below.)
	await expect(root.getByTestId('run-none')).toHaveText(
		'No run is being watched. Select one from the run list.'
	);
	await expect(root.getByTestId('run-row')).toHaveCount(3);
	await expect(
		root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"] [data-testid="run-row-state"]`)
	).toHaveText('Running');
	await expect(
		root.locator(`[data-testid="run-row"][data-run-id="${RUN_C}"] [data-testid="run-row-state"]`)
	).toHaveText('Succeeded');
	await expect(root.getByTestId('runs-total')).toHaveText('Showing 3 of 3 recorded runs.');
	// Host B is a different pinned scope: empty, never A's rows.
	const rootB = page.locator(`${hostB} [data-testid="jobs-root"]`);
	await expect(rootB.getByTestId('jobs-scope')).toHaveText('Server b7e4c2a0');
	await expect(rootB.getByTestId('runs-empty')).toHaveText(
		'No runs are recorded for this server.'
	);
	await expect(rootB.getByTestId('schedules-empty')).toHaveText(
		'No schedules are recorded for this server.'
	);

	// List failure path: explicit error (data-error-code), then Retry
	// recovers — never a faked view.
	await apiCall(page, 'failNextListRuns', [
		{ status: 503, code: 'unavailable', message: 'api unavailable' }
	]);
	await apiCall(page, 'toggleHost', ['A']);
	await apiCall(page, 'toggleHost', ['A']);
	const failed = root.getByTestId('runs-error');
	await expect(failed).toBeVisible({ timeout: 8000 });
	await expect(failed).toHaveAttribute('data-error-code', 'unavailable');
	await root.getByTestId('runs-retry').click();
	await expect(root.getByTestId('run-row')).toHaveCount(3, { timeout: 8000 });

	await apiCall(page, 'failNextListSchedules', [
		{ status: 503, code: 'unavailable', message: 'api unavailable' }
	]);
	await apiCall(page, 'toggleHost', ['A']);
	await apiCall(page, 'toggleHost', ['A']);
	const schedulesFailed = root.getByTestId('schedules-error');
	await expect(schedulesFailed).toBeVisible({ timeout: 8000 });
	await expect(schedulesFailed).toHaveAttribute('data-error-code', 'unavailable');
	await root.getByTestId('schedules-retry').click();
	await expect(root.getByTestId('schedule-row')).toHaveCount(1, { timeout: 8000 });

	// Registry: the real feature entry validates, scopes fail closed, and the
	// host-level guards answer before any panel content mounts.
	const { registry } = await apiState(page);
	expect(registry.kinds).toContain('jobs');
	expect(registry.scopeChecks.serverScope).toBe('ok');
	expect(registry.scopeChecks.globalScope).toBe('scope_kind_not_allowed');
	expect(registry.scopeChecks.unknownKind).toBe('unknown_panel_kind');
	expect(registry.defaultGeometry).toEqual({ x: 96, y: 96, width: 720, height: 560 });
	expect(registry.minGeometry).toEqual({ x: 0, y: 0, width: 460, height: 360 });
	expect(registry.instanceKey).toBe(`jobs::server/${SERVER_A}`);
	await expect(page.locator('section[data-testid="host-global"] [data-testid="panel-unavailable"]')).toBeVisible();
	await expect(
		page.locator(`${'section[data-testid="host-bare"]'} [data-testid="panel-config-error"]`)
	).toBeVisible();
	await expect(page.locator('[data-testid="sibling-panel"]')).toBeVisible();
});

test('schedule create submits the pinned server explicitly; unauthorized target and bad forms stay honest', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
	await expect(root.getByTestId('schedule-row')).toHaveCount(1);
	await expect(root.getByTestId('schedule-form-scope')).toHaveText(
		'New schedule for server 8c1d2e3f (pinned scope).'
	);

	// Client-side form guards: no call, value-free error text.
	await root.getByTestId('schedule-create').click();
	await expect(root.getByTestId('schedule-form-error')).toHaveText('Enter the cron expression.');
	await root.getByTestId('schedule-cron-input').fill('15 3 * * *');
	await root.getByTestId('schedule-create').click();
	await expect(root.getByTestId('schedule-form-error')).toHaveText(
		'Enter the handler name the schedule dispatches.'
	);
	await root.getByTestId('schedule-handler-input').fill('collect.logs');
	await root.getByTestId('schedule-payload-input').fill('{oops');
	await root.getByTestId('schedule-create').click();
	await expect(root.getByTestId('schedule-form-error')).toHaveText(
		'Payload must be a valid JSON object.'
	);
	expect(await callsOf(page, 'createSchedule')).toHaveLength(0);

	// The one submission door: the body carries the PINNED scope, and an
	// empty timezone stays absent (the UTC default is the facade's business).
	await root.getByTestId('schedule-payload-input').fill('{"days":7}');
	await root.getByTestId('schedule-create').click();
	await expect(root.getByTestId('schedule-form-error')).toHaveCount(0);
	await expect(root.getByTestId('schedule-row')).toHaveCount(2);
	await expect(root.getByTestId('schedule-cron-input')).toHaveValue('');
	const creates = await callsOf(page, 'createSchedule');
	const body = recorded<{
		server_id: string;
		cron: string;
		run_spec: { handler_name: string; payload: Record<string, unknown> };
		cron_timezone?: string | null;
	}>(creates[0]?.payload, 'createSchedule payload');
	expect(body.server_id).toBe(SERVER_A);
	expect(body.cron).toBe('15 3 * * *');
	expect(body.run_spec).toEqual({ handler_name: 'collect.logs', payload: { days: 7 } });
	expect('cron_timezone' in body).toBe(false);

	// A different pinned window submits ITS OWN scope — never a shared
	// "current server" (C08 rule 5, NT5).
	const rootB = page.locator(`${hostB} [data-testid="jobs-root"]`);
	await rootB.getByTestId('schedule-cron-input').fill('0 12 * * 1');
	await rootB.getByTestId('schedule-handler-input').fill('rotate.keys');
	await rootB.getByTestId('schedule-payload-input').fill('{}');
	await rootB.getByTestId('schedule-tz-input').fill('Europe/Berlin');
	await rootB.getByTestId('schedule-create').click();
	await expect(rootB.getByTestId('schedule-row')).toHaveCount(1);
	const createsB = await callsOf(page, 'createSchedule');
	const bodyB = recorded<{ server_id: string; cron_timezone?: string | null }>(
		createsB[1]?.payload,
		'second createSchedule payload'
	);
	expect(bodyB.server_id).toBe(SERVER_B);
	expect(bodyB.cron_timezone).toBe('Europe/Berlin');

	// Unauthorized target: the producer denies before any row exists; the
	// panel shows the value-free envelope and adds nothing.
	await apiCall(page, 'failNextCreateSchedule', [
		{ status: 404, code: 'not_found', message: 'no canonical profile for this server id' }
	]);
	await root.getByTestId('schedule-cron-input').fill('0 4 * * *');
	await root.getByTestId('schedule-handler-input').fill('collect.logs');
	await root.getByTestId('schedule-payload-input').fill('{}');
	await root.getByTestId('schedule-create').click();
	await expect(root.getByTestId('schedule-form-error')).toContainText(
		'no canonical profile for this server id'
	);
	await expect(root.getByTestId('schedule-row')).toHaveCount(2);
});

test('schedule pause/resume is the only update; delete removes the row; failures change nothing', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
	const row = root.getByTestId('schedule-row');
	await expect(row).toHaveCount(1);
	await expect(row.getByTestId('schedule-enabled')).toHaveText('Enabled');
	await expect(row.getByTestId('schedule-cron')).toHaveText('0 * * * *');
	await expect(row.getByTestId('schedule-timezone')).toHaveText('UTC');
	await expect(row.getByTestId('schedule-handler')).toHaveText('collect.logs');

	await row.getByTestId('schedule-toggle').click();
	await expect(row.getByTestId('schedule-enabled')).toHaveText('Paused');
	await expect(row.getByTestId('schedule-toggle')).toHaveText('Resume');
	const updates = await callsOf(page, 'updateSchedule');
	const patch = recorded<{ enabled: boolean }>(updates[0]?.payload, 'updateSchedule payload');
	expect(updates[0]?.arg).toBe(SCHED_1);
	expect(patch).toEqual({ enabled: false });

	// A failed pause is an explicit error; the recorded truth is unchanged.
	await apiCall(page, 'failNextUpdateSchedule', [
		{ status: 503, code: 'unavailable', message: 'schedules facade unavailable' }
	]);
	await row.getByTestId('schedule-toggle').click();
	await expect(root.getByTestId('schedule-action-error')).toBeVisible();
	await expect(root.getByTestId('schedule-action-error')).toHaveAttribute(
		'data-error-code',
		'unavailable'
	);
	await expect(row.getByTestId('schedule-enabled')).toHaveText('Paused');

	// A failed delete is an explicit error and removes nothing.
	await apiCall(page, 'failNextDeleteSchedule', [
		{ status: 503, code: 'unavailable', message: 'schedules facade unavailable' }
	]);
	await row.getByTestId('schedule-delete').click();
	await expect(root.getByTestId('schedule-action-error')).toBeVisible();
	await expect(root.getByTestId('schedule-action-error')).toHaveAttribute(
		'data-error-code',
		'unavailable'
	);
	await expect(row).toHaveCount(1);
	await expect(row.getByTestId('schedule-enabled')).toHaveText('Paused');

	// Delete succeeds once the facade answers; the row disappears for good.
	await row.getByTestId('schedule-delete').click();
	await expect(root.getByTestId('schedules-empty')).toHaveText(
		'No schedules are recorded for this server.'
	);
	const deletes = await callsOf(page, 'deleteSchedule');
	expect(deletes[0]?.arg).toBe(SCHED_1);
});

test('watched run replays persisted events without duplicates and reconnects carrying Last-Event-ID', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
	const runRow = root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"]`);

	// Session 1 answers the first subscription (full replay from the start).
	queueStreamSession({
		runId: RUN_A,
		frames: [
			{ seq: 1, type: 'run.submitted' },
			{ seq: 2, type: 'step.started' },
			{ seq: 3, type: 'step.completed' }
		]
	});
	await runRow.getByTestId('run-watch').click();
	await expect(root.getByTestId('watched-state')).toHaveText('Running', { timeout: 8000 });
	await waitForStreamRequests(1);
	expect(streamRequests[0]?.cursor).toBeNull();
	expect(streamRequests[0]?.path).toBe(`/api/v1/jobs/runs/${RUN_A}/events`);
	// (A one-chunk fixture body sets 'live' and 'reconnecting' inside one
	// Svelte flush, so the transient live status is not DOM-observable here;
	// delivery is proven by the rendered rows, status honesty by the
	// reconnecting/closed assertions below.)
	for (const seq of [1, 2, 3]) {
		await expect(
			root.locator(`[data-testid="event-row"][data-event-seq="${seq}"]`)
		).toHaveCount(1);
	}

	// Session 2 arrives after the server's bounded close: the reconnect
	// carries the last seen seq as Last-Event-ID and a re-delivered frame at
	// or below the watermark is dropped — seq 3 stays rendered exactly once.
	await waitForStreamRequests(2);
	expect(streamRequests[1]?.cursor).toBe('3');
	queueStreamSession({
		runId: RUN_A,
		frames: [
			{ seq: 3, type: 'step.completed' },
			{ seq: 4, type: 'step.started' }
		]
	});
	await expect(root.locator('[data-testid="event-row"][data-event-seq="4"]')).toBeVisible({
		timeout: 8000
	});
	await expect(root.locator('[data-testid="event-row"][data-event-seq="3"]')).toHaveCount(1);

	// Session 3 is a connection failure (API restart): the problem is visible
	// and the loop reconnects from the watermark instead of replaying from 0.
	await waitForStreamRequests(3);
	expect(streamRequests[2]?.cursor).toBe('4');
	await apiCall(page, 'armRunScript', [
		RUN_A,
		[
			{
				state: 'succeeded',
				updated_at: '2026-10-15T10:01:00Z',
				steps: [
					{ seq: 1, capability_id: 'host.shell', state: 'succeeded', receipt: null },
					{ seq: 2, capability_id: 'host.shell', state: 'succeeded', receipt: null }
				]
			}
		]
	]);
	queueStreamSession({ runId: RUN_A, abort: true });
	await expect(root.getByTestId('stream-problem')).toContainText('interrupted', {
		timeout: 8000
	});
	await expect(root.getByTestId('stream-status')).toHaveText(
		'Event stream reconnecting from the last recorded event…'
	);

	// Session 4 delivers the terminal transition; the durable refresh replays
	// the succeeded state, the row badge follows, and the loop stops for a
	// terminal run — no further subscription can appear (nothing new can
	// arrive past a terminal state, C05 rule 2).
	await waitForStreamRequests(4);
	expect(streamRequests[3]?.cursor).toBe('4');
	queueStreamSession({
		runId: RUN_A,
		frames: [{ seq: 5, type: 'run.transition' }]
	});
	await expect(root.getByTestId('watched-state')).toHaveText('Succeeded', { timeout: 8000 });
	await expect(root.getByTestId('stream-status')).toHaveText('Event stream closed.');
	await expect(runRow.getByTestId('run-row-state')).toHaveText('Succeeded');
	await expect(root.locator('[data-testid="event-row"][data-event-seq="5"]')).toHaveCount(1);
	await page.waitForTimeout(1200);
	expect(streamRequests).toHaveLength(4);

	// Deduplication receipt: every delivered seq exists exactly once.
	const rendered = await root.getByTestId('event-row').evaluateAll((rows) =>
		rows.map((row) => row.getAttribute('data-event-seq'))
	);
	expect(rendered).toEqual(['1', '2', '3', '4', '5']);
});

test('cancel states are honest: recorded-but-unverified stays pending, verified comes only from the engine answer', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
	const rowA = root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"]`);

	// Pending path: the request is recorded; the outcome is NOT known yet.
	await rowA.getByTestId('run-watch').click();
	await expect(root.getByTestId('watched-state')).toHaveText('Running', { timeout: 8000 });
	await waitForStreamRequests(1);
	await root.getByTestId('watched-cancel').click();
	await expect(root.getByTestId('cancel-pending')).toHaveText(
		'Cancellation requested — not yet verified.'
	);
	await expect(root.getByTestId('watched-state')).toHaveText('Running');
	await expect(root.getByTestId('watched-cancel-note')).toBeVisible();
	await expect(rowA.getByTestId('run-row-cancel-note')).toBeVisible();
	const cancels = await callsOf(page, 'cancelRun');
	expect(cancels[0]?.arg).toBe(RUN_A);
	expect(recorded<{ cancel_children: boolean }>(cancels[0]?.payload, 'cancel payload')).toEqual({
		cancel_children: false
	});

	// Verified path: only the facade's KNOWN engine outcome may say
	// "cancelled" — here the armed answer reports it within the budget.
	const rowD = root.locator(`[data-testid="run-row"][data-run-id="${RUN_D}"]`);
	await rowD.getByTestId('run-watch').click();
	await expect(root.getByTestId('watched-id')).toHaveText(RUN_D.slice(0, 8), { timeout: 8000 });
	await apiCall(page, 'armCancelOutcome', ['cancelled']);
	await root.getByTestId('watched-cancel').click();
	await expect(root.getByTestId('cancel-verified')).toHaveText('Cancelled (verified)');
	await expect(root.getByTestId('watched-state')).toHaveText('Cancelled (verified)');
	await expect(rowD.getByTestId('run-row-state')).toHaveText('Cancelled (verified)');

	// Failure path: a refused cancel is an explicit value-free error; no
	// state is claimed anywhere. (Remount resets the panel; the durable rows
	// persist — RUN_A is still running with its recorded cancel request.)
	await apiCall(page, 'failNextCancel', [
		{ status: 409, code: 'conflict', message: 'the engine has no workflow under this run id' }
	]);
	await apiCall(page, 'toggleHost', ['A']);
	await apiCall(page, 'toggleHost', ['A']);
	await expect(root.getByTestId('run-none')).toBeVisible({ timeout: 8000 });
	await rowA.getByTestId('run-watch').click();
	await expect(root.getByTestId('watched-state')).toHaveText('Running', { timeout: 8000 });
	await expect(root.getByTestId('watched-cancel-note')).toBeVisible();
	await root.getByTestId('watched-cancel').click();
	const cancelError = root.getByTestId('cancel-error');
	await expect(cancelError).toBeVisible();
	await expect(cancelError).toHaveAttribute('data-error-code', 'conflict');
	await expect(root.getByTestId('cancel-pending')).toHaveCount(0);
	await expect(root.getByTestId('cancel-verified')).toHaveCount(0);
	await expect(root.getByTestId('watched-state')).toHaveText('Running');
});

test('hide/unmount releases the stream and refocus starts nothing; closing a window never cancels work', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
	const rowA = root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"]`);

	await rowA.getByTestId('run-watch').click();
	await expect(root.getByTestId('watched-state')).toHaveText('Running', { timeout: 8000 });
	await waitForStreamRequests(1);

	// Hide the window's content: the mount-scoped release path aborts the
	// stream controller. Queue a session afterwards — nothing may consume it,
	// because no subscription may survive the unmount.
	await apiCall(page, 'toggleHost', ['A']);
	await expect(page.locator(`${hostA} [data-testid="jobs-root"]`)).toHaveCount(0);
	await queueStreamSession({ runId: RUN_A, frames: [{ seq: 9, type: 'step.started' }] });
	await page.waitForTimeout(1200);
	expect(streamRequests).toHaveLength(1);

	// Nothing was submitted or cancelled by hiding, and no mutation call
	// exists at all: a closing/hiding window starts and cancels nothing.
	let { calls } = await apiState(page);
	expect(calls.filter((call) => call.op === 'cancelRun')).toHaveLength(0);
	expect(
		calls.filter(
			(call) => !['listRuns', 'listSchedules', 'readRun', 'readServerName'].includes(call.op)
		)
	).toHaveLength(0);

	// Restore: a FRESH mount reloads the lists; the watched run resets to
	// none (no ghost subscription re-arms by itself).
	await apiCall(page, 'toggleHost', ['A']);
	await expect(root.getByTestId('run-none')).toBeVisible({ timeout: 8000 });
	await expect(root.getByTestId('run-row')).toHaveCount(3);
	await page.waitForTimeout(1200);
	expect(streamRequests).toHaveLength(1);
	({ calls } = await apiState(page));
	expect(calls.filter((call) => call.op === 'cancelRun')).toHaveLength(0);

	// Focus change (activating the sibling panel) starts and cancels nothing.
	await page.locator('[data-testid="sibling-panel"]').click();
	await page.waitForTimeout(300);
	({ calls } = await apiState(page));
	expect(calls.filter((call) => call.op === 'cancelRun')).toHaveLength(0);
	expect(calls.filter((call) => call.op === 'createSchedule')).toHaveLength(0);
	expect(calls.filter((call) => call.op === 'updateSchedule')).toHaveLength(0);
	expect(calls.filter((call) => call.op === 'deleteSchedule')).toHaveLength(0);
	expect(streamRequests).toHaveLength(1);
});

// ---------------------------------------------------------------------------
// W3: 10-row pagination + filters, Today bounds, enriched rows, watch report
// ---------------------------------------------------------------------------

test.describe('W3 run list and watch report', () => {
	test.use({ timezoneId: 'Asia/Tokyo' });

	test('10-row pages with load-more dedup; q/date filters ride the wire; Today emits exact local-midnight bounds', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
		await apiCall(page, 'addRuns', [SERVER_A, 24, 'Nightly sweep']);
		// The initial page already loaded with the 3 seeded runs; refresh to
		// page through the full 27-run store.
		await root.getByTestId('runs-refresh').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10);

		// Page 0 asked for exactly 10 rows.
		let calls = await callsOf(page, 'listRuns');
		expect(calls.at(-1)?.payload).toEqual({ limit: 10 });

		// Load more: the CURRENT offset rides the wire; run ids dedup and the
		// filtered total updates; the control disappears when drained.
		await root.getByTestId('runs-load-more').click();
		await expect(root.getByTestId('run-row')).toHaveCount(20, { timeout: 8000 });
		calls = await callsOf(page, 'listRuns');
		expect(calls.at(-1)?.payload).toEqual({ limit: 10, offset: 10 });
		const ids = await root.getByTestId('run-row').evaluateAll((rows) =>
			rows.map((row) => (row as HTMLElement).dataset.runId)
		);
		expect(new Set(ids).size).toBe(20);
		await expect(root.getByTestId('runs-total')).toHaveText('Showing 20 of 27 recorded runs.');
		await root.getByTestId('runs-load-more').click();
		await expect(root.getByTestId('run-row')).toHaveCount(27, { timeout: 8000 });
		await expect(root.getByTestId('runs-load-more')).toHaveCount(0);

		// q filter: applied to page 0, replaces rows, total is the FILTERED total.
		await root.getByTestId('runs-q-input').fill('nightly sweep 0');
		await root.getByTestId('runs-apply-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(9, { timeout: 8000 }); // 01..09
		calls = await callsOf(page, 'listRuns');
		expect(calls.at(-1)?.payload).toEqual({ limit: 10, q: 'nightly sweep 0' });
		await expect(root.getByTestId('runs-total')).toHaveText('Showing 9 of 9 recorded runs.');

		// Explicit date bounds ride the wire verbatim as ISO instants.
		await root.getByTestId('runs-q-input').fill('');
		await root.getByTestId('runs-date-from-input').fill('2026-10-15T09:00:00Z');
		await root.getByTestId('runs-date-to-input').fill('2026-10-15T09:59:59Z');
		await root.getByTestId('runs-apply-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(3, { timeout: 8000 }); // the three seeded runs
		calls = await callsOf(page, 'listRuns');
		expect(calls.at(-1)?.payload).toEqual({
			limit: 10,
			date_from: '2026-10-15T09:00:00Z',
			date_to: '2026-10-15T09:59:59Z'
		});

		// Today preset: browser-LOCAL today (Asia/Tokyo) translated to exact
		// ISO instants — local midnight inclusive, next local midnight as the
		// exclusive upper edge. The emitted bounds are asserted verbatim.
		await page.clock.setFixedTime(new Date('2026-11-05T10:00:00Z'));
		await root.getByTestId('runs-clear-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10, { timeout: 8000 });
		await root.getByTestId('runs-today').click();
		await expect(root.getByTestId('run-row')).toHaveCount(0, { timeout: 8000 }); // no run submitted "today"
		await expect(root.getByTestId('runs-empty')).toHaveText('No runs match the current filters.');
		calls = await callsOf(page, 'listRuns');
		expect(calls.at(-1)?.payload).toEqual({
			limit: 10,
			date_from: '2026-11-04T15:00:00.000Z', // Tokyo local midnight 2026-11-05
			date_to: '2026-11-05T15:00:00.000Z' // Tokyo local midnight 2026-11-06
		});
		// The date inputs carry the same explicit instants (never "today" text).
		await expect(root.getByTestId('runs-date-from-input')).toHaveValue('2026-11-04T15:00:00.000Z');
		await expect(root.getByTestId('runs-date-to-input')).toHaveValue('2026-11-05T15:00:00.000Z');
	});

	test('rows show the human label, full run id, submitted and honest terminal completion', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
		// Label is the primary text; the full run id and full submitted stamp
		// are visible next to it.
		const rowA = root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"]`);
		await expect(rowA.getByTestId('run-row-label')).toHaveText('Collect telemetry');
		await expect(rowA.getByTestId('run-row-id')).toHaveText(RUN_A);
		await expect(rowA.getByTestId('run-row-submitted')).toHaveText(
			'Submitted 2026-10-15T09:00:00Z'
		);
		// A RUNNING row claims no completion (updated_at is not a completion).
		await expect(rowA.getByTestId('run-row-completed')).toHaveCount(0);

		// Terminal rows label updated_at honestly: Completed for succeeded,
		// Ended for failed — never a separate wire-field claim.
		const rowC = root.locator(`[data-testid="run-row"][data-run-id="${RUN_C}"]`);
		await expect(rowC.getByTestId('run-row-completed')).toHaveText(
			'Completed 2026-10-15T09:01:00Z'
		);
		await apiCall(page, 'setRunState', [RUN_A, 'failed', { updated_at: '2026-10-15T09:07:00Z' }]);
		await root.getByTestId('runs-refresh').click();
		await expect(
			root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"] [data-testid="run-row-completed"]`)
		).toHaveText('Ended 2026-10-15T09:07:00Z', { timeout: 8000 });
	});

	test('the pinned server name resolves through the narrow seam; failure keeps the UUID and says so', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
		await expect(root.getByTestId('jobs-scope-name')).toHaveText('Alpha Build Host');
		await expect(root.getByTestId('jobs-scope-name-unavailable')).toHaveCount(0);
		await expect(root.getByTestId('run-row')).toHaveCount(3); // jobs data unaffected

		// Name lookup failure is nonfatal: UUID stays, honest note, list works.
		const rootB = page.locator(`${hostB} [data-testid="jobs-root"]`);
		await expect(rootB.getByTestId('jobs-scope-name')).toHaveCount(0);
		await expect(rootB.getByTestId('jobs-scope-name-unavailable')).toHaveText(
			'Server name unavailable — showing the pinned id.'
		);
		await expect(rootB.getByTestId('jobs-scope')).toHaveText('Server b7e4c2a0');
		await expect(rootB.getByTestId('schedules-empty')).toHaveText(
			'No schedules are recorded for this server.'
		);
	});

	test('watch report renders the final answer, tool results and failure reason; deltas and thinking never surface', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);

		// The report section exists only while a run is watched; before any
		// final round arrives it says so honestly.
		await root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"] [data-testid="run-watch"]`).click();
		await expect(root.getByTestId('watched-state')).toHaveText('Running', { timeout: 8000 });
		await expect(root.getByTestId('watch-report-empty')).toHaveText(
			'No final agent response is recorded for this run yet.'
		);
		// Replay: the deltas must never appear as content; the final round is
		// the current answer; the tool result carries capability + text; the
		// failure reason is visibly useful.
		const secretDelta = 'SECRET-DELTA-CONTENT-7f3a';
		const secretThinking = 'SECRET-THINKING-CONTENT-91bc';
		queueStreamSession({
			runId: RUN_A,
			frames: [
				{
					seq: 1,
					type: 'agent.output_delta',
					data: { output: secretDelta, thinking: secretThinking }
				},
				{
					seq: 2,
					type: 'agent.model_round',
					data: { status: 'final', output: 'Deploy finished.\nAll 4 hosts green.' }
				},
				{
					seq: 3,
					type: 'agent.tool_result',
					data: {
						capability_id: 'host.shell',
						result: { exit_code: 0, stdout: 'ok' }
					}
				},
				{
					seq: 4,
					type: 'agent.failed',
					data: { reason: 'verify step timed out after 30s' }
				}
			]
		});
		const finalText = root.getByTestId('watch-report-final');
		await expect(finalText).toHaveText('Deploy finished.\nAll 4 hosts green.', {
			timeout: 8000,
			useInnerText: true
		});
		// Whitespace preserved (pre-wrap) and the final round is honest about
		// its provenance timestamp.
		await expect(finalText).toHaveCSS('white-space', 'pre-wrap');
		await expect(root.getByTestId('watch-report-meta')).toContainText('Final response ·');
		// Tool result: capability + bounded useful JSON text.
		const toolRow = root.getByTestId('tool-result-row').first();
		await expect(toolRow.getByTestId('tool-result-capability')).toHaveText('host.shell');
		await expect(toolRow.getByTestId('tool-result-text')).toHaveText(
			'{"exit_code":0,"stdout":"ok"}'
		);
		// Failure reason is visibly useful.
		await expect(root.getByTestId('watch-failed')).toHaveText(
			'Failure reason: verify step timed out after 30s'
		);
		// Delta content and thinking NEVER render — as content or anywhere.
		const body = await page.locator(`${hostA} [data-testid="jobs-root"]`).innerText();
		expect(body).not.toContain(secretDelta);
		expect(body).not.toContain(secretThinking);
		// The delta frame appears only as a compact lifecycle line (no payload).
		const deltaRows = root.locator('[data-testid="event-row"]', {
			hasText: 'Streaming delta (transient)'
		});
		await expect(deltaRows).toHaveCount(1);
		expect((await deltaRows.first().innerText()).includes(secretDelta)).toBe(false);
	});

	test('replay stays bounded: 520 delivered events render at most 500 rows', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
		// RUN_A is running — a terminal run's stream never opens (nothing new
		// can arrive), so the bounded-replay proof needs a live target.
		await root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"] [data-testid="run-watch"]`).click();
		const frames = Array.from({ length: 520 }, (_, index) => ({
			seq: index + 1,
			type: 'run.transition',
			data: {}
		}));
		queueStreamSession({ runId: RUN_A, frames });
		await expect(
			page.locator(`${hostA} [data-testid="event-row"]`)
		).toHaveCount(500, { timeout: 15000 });
		// The watermark sits at the LAST delivered seq (520), not the cap.
		await expect(
			page.locator(`${hostA} [data-testid="event-row"][data-event-seq="520"]`)
		).toHaveCount(1);
	});
});
