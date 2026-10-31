import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-05-web-jobs — real-browser proofs for the jobs & schedules panel.
 *
 * Harness: the REAL feature panel (`JobsPanel.svelte`), the REAL feature
 * entry (`05-web-jobs.ts`), the REAL panel host/registry, and the REAL
 * transport — `createMaiApiClient()` composed through `jobsServices` exactly
 * like the desktop, plus the REAL `browserRunEventStream` SSE binding. Every
 * REST byte rides HTTP and is answered by Playwright route handlers at the
 * external boundary (history-tags delivery contract: no in-page API stub):
 * the fixture mirrors the reviewed producers 1:1, records every request
 * (method, path, query, mutation body, CSRF header) and serves the C01
 * envelope shapes, so request query evidence and explicit server scope are
 * asserted on the wire, not against an in-process double.
 *
 * The SSE stream keeps the reviewed wire control: the panel's subscription
 * goes through `subscribeEvents` and is answered by route handlers, so the
 * concrete producer URL, the `Last-Event-ID` cursor carry, frame parsing and
 * reconnect behavior are exercised for real (C05 rule 4, C08 rule 7).
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

/** Deterministic catalog identity: 52 tags, `tag-00` … `tag-51`. */
const CATALOG_SIZE = 52;
function tagId(index: number): string {
	return `a0a0a0a0-0000-4000-8000-${String(index).padStart(12, '0')}`;
}
function tagName(index: number): string {
	return `tag-${String(index).padStart(2, '0')}`;
}
const TAG_00 = tagId(0);
const TAG_01 = tagId(1);
const TAG_02 = tagId(2);
const TAG_03 = tagId(3);

type Fail = { status: number; code: string; message: string };

/**
 * In-page control surface kept INSIDE the fixture app: only the knob that is
 * genuinely in-page UI state (the host A visibility toggle) plus the registry
 * summary computed at mount. Every data-plane fixture lives in the Node route
 * handlers (the external HTTP boundary); the panel talks to it ONLY through
 * the real `createMaiApiClient()` transport.
 */
interface JobsWindowApi {
	toggleHost(host: 'A' | 'B'): void;
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
 * scope rejection) and one benign sibling panel. NO fixture data plane lives
 * here: the panel's services bag is the real client composition.
 */
const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry, definePanelEntry, singletonInstanceKey } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { createMaiApiClient } from '${CLIENT}';
	import {
		jobsEntry,
		jobsServices,
		browserRunEventStream,
		JOBS_PANEL_KIND
	} from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';

	// The real transport: the double-submit cookie the client reads for
	// mutation headers; every REST byte then rides HTTP and is answered by
	// the external route handlers (nothing here fakes the data plane).
	document.cookie = 'mai_csrf=fixture-csrf-token; path=/';
	const client = createMaiApiClient();
	const services = jobsServices(
		Object.assign({}, client, {
			streamRunEvents: browserRunEventStream,
			// W3 narrow name seam, composed exactly like the desktop: resolve
			// the pinned scope's enrolled profile name; failure stays nonfatal.
			readServerName: async (serverId) => (await client.readProfile(serverId)).name
		})
	);

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
		toggleHost(host) {
			if (host === 'A') showA = !showA;
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
// External HTTP fixture: the reviewed producers mirrored 1:1 in Node, at the
// one boundary the panel is allowed to know. Every request is recorded with
// its query (scope + filter evidence) and mutation body/CSRF header.
// ---------------------------------------------------------------------------

interface RunRow {
	run_id: string;
	server_id: string;
	parent_run_id: string | null;
	depth: number;
	profile_revision: number;
	prompt_revision: number;
	model_profile: string;
	requested_grants: string[];
	label: string;
	state: string;
	cancel_requested_at: string | null;
	submitted_at: string;
	updated_at: string;
	steps: Array<{ seq: number; capability_id: string; state: string; receipt: string | null }>;
}

interface ScheduleRow {
	schedule_id: string;
	server_id: string;
	cron: string;
	cron_timezone: string;
	misfire_policy: string;
	enabled: boolean;
	run_spec: { handler_name: string; payload: Record<string, unknown> };
	created_at: string;
	updated_at: string;
}

interface TagRow {
	tag_id: string;
	name: string;
	description: string | null;
	created_at: string;
	updated_at: string;
}

interface AssignmentRow {
	tag_id: string;
	name: string;
	assigned_by: string;
	assignment_source: string;
	created_at: string;
}

interface RecordedRequest {
	method: string;
	path: string;
	search: string;
}
interface RecordedMutation extends RecordedRequest {
	body: unknown;
	csrf: string | null;
}

let runsStore: Record<string, Record<string, RunRow>> = {};
let schedulesStore: Record<string, ScheduleRow[]> = {};
let catalog: TagRow[] = [];
/** Per-run associated tag ids (insertion order; display order is name asc). */
let runTags: Record<string, string[]> = {};
let runScripts: Record<string, Array<Record<string, unknown>>> = {};
let restLog: RecordedRequest[] = [];
let restBodies: RecordedMutation[] = [];
let requestCounter = 0;
let fails: Record<string, Fail | null> = {};
let cancelOutcomeArmed: string | null = null;

function step(seq: number, state: string): RunRow['steps'][number] {
	return { seq, capability_id: 'host.shell', state, receipt: null };
}

function runRow(
	runId: string,
	serverId: string,
	state: string,
	steps: RunRow['steps'],
	overrides?: Partial<RunRow>
): RunRow {
	return {
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
		steps,
		...overrides
	};
}

function scheduleRow(
	scheduleId: string,
	serverId: string,
	cron: string,
	timezone: string,
	handler: string,
	enabled: boolean
): ScheduleRow {
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

/** The mandatory bounded preview: first ≤3 names in deterministic order. */
function previewOf(runId: string): { preview: string[]; total: number } {
	const ids = runTags[runId] ?? [];
	const names = ids
		.map((id) => catalog.find((tag) => tag.tag_id === id)?.name ?? '')
		.filter((name) => name !== '')
		.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
	return { preview: names.slice(0, 3), total: names.length };
}

/** One wire RunView: the durable row minus steps, plus the honest preview. */
function runViewOf(row: RunRow): Record<string, unknown> {
	const { steps: _steps, ...rest } = row;
	return { ...rest, tag_preview: previewOf(row.run_id) };
}

function findRun(runId: string): RunRow | null {
	for (const serverRows of Object.values(runsStore)) {
		if (serverRows[runId]) return serverRows[runId];
	}
	return null;
}

function findSchedule(scheduleId: string): ScheduleRow | null {
	for (const rows of Object.values(schedulesStore)) {
		const row = rows.find((candidate) => candidate.schedule_id === scheduleId);
		if (row) return row;
	}
	return null;
}

/** Deterministic association rows of one run, name-ascending. */
function assignmentRowsOf(runId: string): AssignmentRow[] {
	const ids = runTags[runId] ?? [];
	return ids
		.map((id) => {
			const tag = catalog.find((candidate) => candidate.tag_id === id);
			if (!tag) return null;
			return {
				tag_id: tag.tag_id,
				name: tag.name,
				assigned_by: '',
				assignment_source: 'system',
				created_at: tag.created_at
			};
		})
		.filter((row): row is AssignmentRow => row !== null)
		.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

/**
 * Adds deterministic paginated rows (the reviewed W3 fixture shape): distinct
 * submitted_at for stable ordering, unique ids, useful labels.
 */
function addRuns(serverId: string, count: number, prefix: string): void {
	const store = runsStore[serverId];
	if (!store) return;
	for (let index = 0; index < count; index += 1) {
		const runId = crypto.randomUUID();
		store[runId] = runRow(runId, serverId, 'succeeded', [], {
			label: prefix + ' ' + String(index + 1).padStart(2, '0'),
			submitted_at: '2026-10-15T10:' + String(10 + index).padStart(2, '0') + ':00Z',
			updated_at: '2026-10-15T10:' + String(10 + index).padStart(2, '0') + ':30Z'
		});
	}
}

/**
 * The tag-filter pagination fixture: 12 tag-bearing runs on SERVER_A, each
 * associated with tag-02 AND tag-03 so any/all combinations produce >10 rows
 * (Load more must survive the tag filter).
 */
function seedTaggedSweeps(): void {
	const store = runsStore[SERVER_A];
	for (let index = 0; index < 12; index += 1) {
		const runId = `2b2b2b2b-0000-4000-8000-${String(index).padStart(12, '0')}`;
		store[runId] = runRow(runId, SERVER_A, 'succeeded', [], {
			label: 'Tagged sweep ' + String(index + 1).padStart(2, '0'),
			submitted_at: '2026-10-15T10:' + String(30 + index).padStart(2, '0') + ':00Z',
			updated_at: '2026-10-15T10:' + String(30 + index).padStart(2, '0') + ':30Z'
		});
		runTags[runId] = [TAG_02, TAG_03];
	}
}

function jsonReply(route: Route, status: number, body: unknown): Promise<void> {
	return route.fulfill({
		status,
		contentType: 'application/json',
		body: JSON.stringify(body)
	});
}

function envelope(route: Route, status: number, code: string, message: string): Promise<void> {
	requestCounter += 1;
	return jsonReply(route, status, {
		error: { code, message, request_id: `fixture-req-${requestCounter}` }
	});
}

function voidReply(route: Route, status: number): Promise<void> {
	return route.fulfill({ status });
}

/** One-shot failure or the recorded next response for a fixture op. */
function consumeFail(name: string, route: Route): Promise<void> | null {
	const failure = fails[name];
	if (!failure) return null;
	fails[name] = null;
	return envelope(route, failure.status, failure.code, failure.message);
}

function slicePage<T>(
	rows: T[],
	search: URLSearchParams
): { items: T[]; limit: number; offset: number; total: number; has_more: boolean } {
	const limit = Number(search.get('limit') ?? '50');
	const offset = Number(search.get('offset') ?? '0');
	const total = rows.length;
	return {
		items: rows.slice(offset, offset + limit),
		limit,
		offset,
		total,
		has_more: offset + limit < total
	};
}

/**
 * The runs list: explicit server scope REQUIRED (C01 rule 7), the W1 q/date
 * filters and the HT-W6B tag filter (repeated tag_id, ≤50, any/all) all
 * apply BEFORE pagination — the pre-slice total is filtered.
 */
async function handleRest(route: Route): Promise<void> {
	const request = route.request();
	const url = new URL(request.url());
	const method = request.method();
	const path = url.pathname;
	const search = url.searchParams;
	const isMutation = method !== 'GET';
	if (isMutation) {
		let body: unknown = null;
		try {
			body = request.postDataJSON();
		} catch {
			body = null;
		}
		restBodies.push({ method, path, search: url.search, body, csrf: request.headers()['x-csrf-token'] ?? null });
	} else {
		restLog.push({ method, path, search: url.search });
	}

	// ---- global tag catalog (HT-W6B) ---------------------------------------
	if (method === 'GET' && path === '/api/v1/tags') {
		const failed = consumeFail('listTags', route);
		if (failed) return failed;
		const needle = (search.get('name_filter') ?? '').toLowerCase();
		const rows = catalog
			.filter((tag) => (needle === '' ? true : tag.name.toLowerCase().includes(needle)))
			.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
		const page = slicePage(rows, search);
		return jsonReply(route, 200, {
			limit: page.limit,
			offset: page.offset,
			total: page.total,
			has_more: page.has_more,
			items: page.items
		});
	}

	// ---- enrolled profile (the W3 narrow name seam) -------------------------
	const profileMatch = path.match(/^\/api\/v1\/servers\/([^/]+)$/);
	if (method === 'GET' && profileMatch) {
		const serverId = decodeURIComponent(profileMatch[1]);
		if (serverId === SERVER_A) {
			return jsonReply(route, 200, {
				server_id: SERVER_A,
				name: 'Alpha Build Host',
				description: null,
				created_at: '2026-10-01T00:00:00Z',
				updated_at: '2026-10-01T00:00:00Z'
			});
		}
		return envelope(route, 404, 'not_found', 'no canonical profile for this server id');
	}

	// ---- runs list -----------------------------------------------------------
	if (method === 'GET' && path === '/api/v1/jobs/runs') {
		const failed = consumeFail('listRuns', route);
		if (failed) return failed;
		const serverId = search.get('server_id');
		if (serverId === null || serverId === '') {
			return envelope(route, 422, 'request_rejected', 'server_id is required');
		}
		let rows = Object.values(runsStore[serverId] ?? {}).map(runViewOf);
		const q = search.get('q');
		if (q !== null && q !== '') {
			const needle = q.toLowerCase();
			rows = rows.filter(
				(row) =>
					String(row.run_id).toLowerCase().includes(needle) ||
					String(row.label ?? '').toLowerCase().includes(needle) ||
					String(row.model_profile ?? '').toLowerCase().includes(needle)
			);
		}
		const dateFrom = search.get('date_from');
		if (dateFrom !== null && dateFrom !== '') {
			rows = rows.filter((row) => String(row.submitted_at) >= dateFrom);
		}
		const dateTo = search.get('date_to');
		if (dateTo !== null && dateTo !== '') {
			rows = rows.filter((row) => String(row.submitted_at) <= dateTo);
		}
		const tagIds = search.getAll('tag_id');
		if (tagIds.length > 50) {
			return envelope(route, 422, 'request_rejected', 'at most 50 tag_id values are allowed');
		}
		if (tagIds.length > 0) {
			const matchAll = search.get('tag_match') === 'all';
			rows = rows.filter((row) => {
				const attached = new Set(runTags[String(row.run_id)] ?? []);
				return matchAll
					? tagIds.every((id) => attached.has(id))
					: tagIds.some((id) => attached.has(id));
			});
		}
		rows.sort((a, b) => {
			if (String(a.submitted_at) !== String(b.submitted_at)) {
				return String(a.submitted_at) < String(b.submitted_at) ? -1 : 1;
			}
			return String(a.run_id) < String(b.run_id) ? -1 : 1;
		});
		const page = slicePage(rows, search);
		return jsonReply(route, 200, {
			runs: page.items,
			limit: page.limit,
			offset: page.offset,
			total: page.total
		});
	}

	// ---- one run -------------------------------------------------------------
	const cancelMatch = path.match(/^\/api\/v1\/jobs\/runs\/([^/]+)\/cancel$/);
	if (method === 'POST' && cancelMatch) {
		const failed = consumeFail('cancel', route);
		if (failed) return failed;
		const runId = decodeURIComponent(cancelMatch[1]);
		const row = findRun(runId);
		if (!row) return envelope(route, 404, 'not_found', 'no run stored for this id');
		if (cancelOutcomeArmed !== null) {
			row.state = cancelOutcomeArmed;
			row.updated_at = '2026-10-15T09:06:00Z';
			return jsonReply(route, 200, {
				run_id: runId,
				state: row.state,
				outcome: row.state,
				requested_at: '2026-10-15T09:05:00Z',
				outcome_at: '2026-10-15T09:06:00Z',
				requested_now: true,
				unresolved_seqs: []
			});
		}
		// Real boundary behavior: the request is recorded ONCE and the budget
		// expires into an honest pending — outcome null never claims success.
		row.cancel_requested_at = '2026-10-15T09:05:00Z';
		return jsonReply(route, 200, {
			run_id: runId,
			state: row.state,
			outcome: null,
			requested_at: '2026-10-15T09:05:00Z',
			outcome_at: null,
			requested_now: true,
			unresolved_seqs: []
		});
	}

	// ---- run tag associations (HT-W6B job tag routes) ------------------------
	const tagCollectionMatch = path.match(/^\/api\/v1\/jobs\/runs\/([^/]+)\/tags$/);
	if (tagCollectionMatch && (method === 'GET' || method === 'POST')) {
		const runId = decodeURIComponent(tagCollectionMatch[1]);
		const row = findRun(runId);
		if (method === 'GET') {
			const failed = consumeFail('listRunTags', route);
			if (failed) return failed;
			if (!row) return envelope(route, 404, 'not_found', 'no run stored for this id');
			const scope = search.get('server_id');
			// Same-server scope: the entity is resolved INSIDE the named scope.
			if (scope !== row.server_id) {
				return envelope(route, 404, 'not_found', 'no such run in this server scope');
			}
			const rows = assignmentRowsOf(runId);
			const page = slicePage(rows, search);
			const items = [...page.items];
			// Pagination dedup fixture: RUN_A's SECOND page re-serves one stray
			// duplicate row (tag-00) — the popup must dedup by tag_id and still
			// land on an honest 12/12 with Load more hidden.
			if (runId === RUN_A && page.offset > 0 && items.length > 0) {
				items.push(items[0]!);
			}
			return jsonReply(route, 200, {
				run_id: runId,
				limit: page.limit,
				offset: page.offset,
				total: page.total,
				has_more: page.has_more,
				items
			});
		}
		const failed = consumeFail('assignRunTag', route);
		if (failed) return failed;
		if (!row) return envelope(route, 404, 'not_found', 'no run stored for this id');
		const body = (restBodies.at(-1)?.body ?? {}) as { tag_id?: string; server_id?: string };
		if (body.server_id !== row.server_id) {
			return envelope(route, 404, 'not_found', 'no such run in this server scope');
		}
		const tag = catalog.find((candidate) => candidate.tag_id === body.tag_id);
		if (!tag) return envelope(route, 404, 'tag_not_found', 'no such catalog tag');
		const attached = (runTags[runId] ??= []);
		if (!attached.includes(tag.tag_id)) {
			attached.push(tag.tag_id);

		}
		return voidReply(route, 204);
	}

	const tagMemberMatch = path.match(/^\/api\/v1\/jobs\/runs\/([^/]+)\/tags\/([^/]+)$/);
	if (tagMemberMatch && method === 'DELETE') {
		const failed = consumeFail('removeRunTag', route);
		if (failed) return failed;
		const runId = decodeURIComponent(tagMemberMatch[1]);
		const tagIdParam = decodeURIComponent(tagMemberMatch[2]);
		const row = findRun(runId);
		if (!row) return envelope(route, 404, 'not_found', 'no run stored for this id');
		if (search.get('server_id') !== row.server_id) {
			return envelope(route, 404, 'not_found', 'no such run in this server scope');
		}
		const attached = (runTags[runId] ??= []);
		const index = attached.indexOf(tagIdParam);
		if (index === -1) return voidReply(route, 204); // idempotent no-op
		const replacement = search.get('replacement_tag_id');
		if (attached.length === 1) {
			if (replacement === null || replacement === '') {
				return envelope(
					route,
					409,
					'last_tag',
					"the run's final tag cannot be removed without a replacement"
				);
			}
			if (replacement === tagIdParam) {
				return envelope(route, 422, 'request_rejected', 'the replacement must differ from the removed tag');
			}
			if (!catalog.some((candidate) => candidate.tag_id === replacement)) {
				return envelope(route, 404, 'tag_not_found', 'no such replacement tag');
			}
		}
		attached.splice(index, 1);
		if (replacement !== null && replacement !== '' && attached.length === 0) {
			attached.push(replacement);
		}

		return voidReply(route, 204);
	}

	// ---- one run read (detail with durable steps) ----------------------------
	const runMatch = path.match(/^\/api\/v1\/jobs\/runs\/([^/]+)$/);
	if (method === 'GET' && runMatch) {
		const failed = consumeFail('readRun', route);
		if (failed) return failed;
		const runId = decodeURIComponent(runMatch[1]);
		const row = findRun(runId);
		if (!row) return envelope(route, 404, 'not_found', 'no run stored for this id');
		const script = runScripts[runId];
		if (script && script.length > 0) {
			// The facade replays persisted fields of the SAME durable row.
			Object.assign(row, script.shift());
		}
		return jsonReply(route, 200, { ...runViewOf(row), steps: row.steps });
	}

	// ---- schedules -----------------------------------------------------------
	if (method === 'GET' && path === '/api/v1/schedules') {
		const failed = consumeFail('listSchedules', route);
		if (failed) return failed;
		const serverId = search.get('server_id');
		const rows = schedulesStore[serverId ?? ''] ?? [];
		const page = slicePage(rows, search);
		return jsonReply(route, 200, {
			schedules: page.items,
			limit: page.limit,
			offset: page.offset,
			total: page.total
		});
	}
	if (method === 'POST' && path === '/api/v1/schedules') {
		const failed = consumeFail('createSchedule', route);
		if (failed) return failed;
		const payload = (restBodies.at(-1)?.body ?? {}) as {
			server_id?: string;
			cron?: string;
			cron_timezone?: string;
			run_spec?: { handler_name: string; payload: Record<string, unknown> };
		};
		if (!(runsStore[payload.server_id ?? ''] || schedulesStore[payload.server_id ?? ''])) {
			return envelope(route, 404, 'not_found', 'no canonical profile for this server id');
		}
		const row: ScheduleRow = {
			schedule_id: crypto.randomUUID(),
			server_id: payload.server_id ?? '',
			cron: payload.cron ?? '',
			cron_timezone: payload.cron_timezone ?? 'UTC',
			misfire_policy: 'skip_missed',
			enabled: true,
			run_spec: JSON.parse(JSON.stringify(payload.run_spec ?? { handler_name: '', payload: {} })),
			created_at: '2026-10-15T09:10:00Z',
			updated_at: '2026-10-15T09:10:00Z'
		};
		schedulesStore[payload.server_id ?? ''].push(row);
		return jsonReply(route, 200, row);
	}
	const scheduleMemberMatch = path.match(/^\/api\/v1\/schedules\/([^/]+)$/);
	if (scheduleMemberMatch && (method === 'PATCH' || method === 'DELETE')) {
		const scheduleId = decodeURIComponent(scheduleMemberMatch[1]);
		const row = findSchedule(scheduleId);
		if (!row) return envelope(route, 404, 'not_found', 'no schedule stored for this id');
		if (method === 'PATCH') {
			const failed = consumeFail('updateSchedule', route);
			if (failed) return failed;
			const payload = (restBodies.at(-1)?.body ?? {}) as { enabled?: boolean };
			row.enabled = payload.enabled ?? row.enabled;
			row.updated_at = '2026-10-15T09:11:00Z';
			return jsonReply(route, 200, row);
		}
		const failed = consumeFail('deleteSchedule', route);
		if (failed) return failed;
		const rows = schedulesStore[row.server_id];
		rows.splice(
			rows.findIndex((candidate) => candidate.schedule_id === scheduleId),
			1
		);
		return voidReply(route, 204);
	}

	return envelope(route, 404, 'not_found', `fixture has no handler for ${method} ${path}`);
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
	// Fresh data plane per test: the reviewed 3-run store, one schedule, a
	// 52-tag catalog and deterministic seed associations.
	runsStore = {
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
	schedulesStore = {
		[SERVER_A]: [scheduleRow(SCHED_1, SERVER_A, '0 * * * *', 'UTC', 'collect.logs', true)],
		[SERVER_B]: []
	};
	catalog = Array.from({ length: CATALOG_SIZE }, (_, index) => ({
		tag_id: tagId(index),
		name: tagName(index),
		description: null,
		created_at: '2026-10-01T00:00:00Z',
		updated_at: '2026-10-01T00:00:00Z'
	}));
	// Mandatory-tag invariant: every seeded run carries ≥1 tag (no zero-tag
	// fabrication); RUN_A is the pagination fixture with 12 associations.
	runTags = {
		[RUN_A]: Array.from({ length: 12 }, (_, index) => tagId(index)),
		[RUN_C]: [TAG_00],
		[RUN_D]: [TAG_03]
	};
	runScripts = {};
	restLog = [];
	restBodies = [];
	requestCounter = 0;
	fails = {
		listRuns: null,
		listSchedules: null,
		createSchedule: null,
		readRun: null,
		cancel: null,
		updateSchedule: null,
		deleteSchedule: null,
		listTags: null,
		listRunTags: null,
		assignRunTag: null,
		removeRunTag: null
	};
	cancelOutcomeArmed = null;

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

	// Route order (Playwright answers with the LAST matching registration):
	// fixture assets first, then the REST boundary, then the SSE route wins
	// for the event-stream URLs.
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
	await page.route('**/api/v1/**', (route) => handleRest(route));
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
 * Fixture-API boundary. Only the in-page UI knob (host A visibility) crosses
 * into the page; every data-plane assertion reads the Node-side request log.
 */
async function apiCall(page: Page, method: 'toggleHost', args: ('A' | 'B')[]): Promise<void> {
	const pair: [string, string[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiJobsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		const fn = (value as unknown as Record<string, (...a: string[]) => void>)[name];
		if (typeof fn !== 'function') throw new Error(`unknown fixture method ${name}`);
		fn.apply(value, callArgs);
	}, pair);
}

interface RegistrySummary {
	kinds: string[];
	scopeChecks: { serverScope: string; globalScope: string; unknownKind: string };
	defaultGeometry: unknown;
	minGeometry: unknown;
	instanceKey: string | null;
}

/** Snapshot the precomputed registry summary from the page. */
async function registryOf(page: Page): Promise<RegistrySummary> {
	return page.evaluate(() => {
		const value = window.__maiJobsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		return value.registry;
	});
}

/** Recorded requests matching method + path prefix (wire evidence). */
function restCalls(method: string, path: string): RecordedRequest[] {
	return restLog.filter((entry) => entry.method === method && entry.path === path);
}

function restMutations(method: string, path: string): RecordedMutation[] {
	return restBodies.filter((entry) => entry.method === method && entry.path === path);
}

/** The LAST recorded query of one GET route as a typed searchParams view. */
function lastQuery(method: string, path: string): URLSearchParams {
	const entry = restCalls(method, path).at(-1);
	if (!entry) throw new Error(`no recorded ${method} ${path}`);
	return new URLSearchParams(entry.search);
}

/** Compact query shape for toEqual assertions (only the params present). */
function queryShape(entry: RecordedRequest): Record<string, string | string[]> {
	const params = new URLSearchParams(entry.search);
	const shape: Record<string, string | string[]> = {};
	for (const key of new Set(params.keys())) {
		const all = params.getAll(key);
		shape[key] = all.length > 1 ? all : all[0];
	}
	return shape;
}

const hostA = 'section[data-testid="host-a"]';
const hostB = 'section[data-testid="host-b"]';

test('explicit scope renders honest list states; the entry registers and scopes fail closed', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
	await expect(root.getByTestId('jobs-scope')).toHaveText('Server 8c1d2e3f');
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
	// Scope evidence on the wire: every list read names its explicit target.
	const listQuery = lastQuery('GET', '/api/v1/jobs/runs');
	expect(listQuery.get('server_id')).toBe(SERVER_A);
	expect(listQuery.get('limit')).toBe('10');
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
	fails.listRuns = { status: 503, code: 'unavailable', message: 'api unavailable' };
	await apiCall(page, 'toggleHost', ['A']);
	await apiCall(page, 'toggleHost', ['A']);
	const failed = root.getByTestId('runs-error');
	await expect(failed).toBeVisible({ timeout: 8000 });
	await expect(failed).toHaveAttribute('data-error-code', 'unavailable');
	await root.getByTestId('runs-retry').click();
	await expect(root.getByTestId('run-row')).toHaveCount(3, { timeout: 8000 });

	fails.listSchedules = { status: 503, code: 'unavailable', message: 'api unavailable' };
	await apiCall(page, 'toggleHost', ['A']);
	await apiCall(page, 'toggleHost', ['A']);
	const schedulesFailed = root.getByTestId('schedules-error');
	await expect(schedulesFailed).toBeVisible({ timeout: 8000 });
	await expect(schedulesFailed).toHaveAttribute('data-error-code', 'unavailable');
	await root.getByTestId('schedules-retry').click();
	await expect(root.getByTestId('schedule-row')).toHaveCount(1, { timeout: 8000 });

	// Registry: the real feature entry validates, scopes fail closed, and the
	// host-level guards answer before any panel content mounts.
	const registry = await registryOf(page);
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
	expect(restMutations('POST', '/api/v1/schedules')).toHaveLength(0);

	// The one submission door: the body carries the PINNED scope, and an
	// empty timezone stays absent (the UTC default is the facade's business).
	await root.getByTestId('schedule-payload-input').fill('{"days":7}');
	await root.getByTestId('schedule-create').click();
	await expect(root.getByTestId('schedule-form-error')).toHaveCount(0);
	await expect(root.getByTestId('schedule-row')).toHaveCount(2);
	await expect(root.getByTestId('schedule-cron-input')).toHaveValue('');
	const creates = restMutations('POST', '/api/v1/schedules');
	expect(creates).toHaveLength(1);
	const body = creates[0]?.body as {
		server_id: string;
		cron: string;
		run_spec: { handler_name: string; payload: Record<string, unknown> };
		cron_timezone?: string | null;
	};
	expect(body.server_id).toBe(SERVER_A);
	expect(body.cron).toBe('15 3 * * *');
	expect(body.run_spec).toEqual({ handler_name: 'collect.logs', payload: { days: 7 } });
	expect('cron_timezone' in body).toBe(false);
	// Mutation wire hygiene: the double-submit CSRF header rides the POST.
	expect(creates[0]?.csrf).toBe('fixture-csrf-token');

	// A different pinned window submits ITS OWN scope — never a shared
	// "current server" (C08 rule 5, NT5).
	const rootB = page.locator(`${hostB} [data-testid="jobs-root"]`);
	await rootB.getByTestId('schedule-cron-input').fill('0 12 * * 1');
	await rootB.getByTestId('schedule-handler-input').fill('rotate.keys');
	await rootB.getByTestId('schedule-payload-input').fill('{}');
	await rootB.getByTestId('schedule-tz-input').fill('Europe/Berlin');
	await rootB.getByTestId('schedule-create').click();
	await expect(rootB.getByTestId('schedule-row')).toHaveCount(1);
	const createsB = restMutations('POST', '/api/v1/schedules');
	const bodyB = createsB[1]?.body as { server_id: string; cron_timezone?: string | null };
	expect(bodyB.server_id).toBe(SERVER_B);
	expect(bodyB.cron_timezone).toBe('Europe/Berlin');

	// Unauthorized target: the producer denies before any row exists; the
	// panel shows the value-free envelope and adds nothing.
	fails.createSchedule = {
		status: 404,
		code: 'not_found',
		message: 'no canonical profile for this server id'
	};
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
	const updates = restMutations('PATCH', `/api/v1/schedules/${SCHED_1}`);
	expect(updates).toHaveLength(1);
	expect(updates[0]?.body).toEqual({ enabled: false });

	// A failed pause is an explicit error; the recorded truth is unchanged.
	fails.updateSchedule = { status: 503, code: 'unavailable', message: 'schedules facade unavailable' };
	await row.getByTestId('schedule-toggle').click();
	await expect(root.getByTestId('schedule-action-error')).toBeVisible();
	await expect(root.getByTestId('schedule-action-error')).toHaveAttribute(
		'data-error-code',
		'unavailable'
	);
	await expect(row.getByTestId('schedule-enabled')).toHaveText('Paused');

	// A failed delete is an explicit error and removes nothing.
	fails.deleteSchedule = { status: 503, code: 'unavailable', message: 'schedules facade unavailable' };
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
	expect(restMutations('DELETE', `/api/v1/schedules/${SCHED_1}`)).toHaveLength(2);
});

test('watched run replays persisted events without duplicates and reconnects carrying Last-Event-ID', async ({
	page
}) => {
	const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
	const runRowLoc = root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"]`);

	// Session 1 answers the first subscription (full replay from the start).
	queueStreamSession({
		runId: RUN_A,
		frames: [
			{ seq: 1, type: 'run.submitted' },
			{ seq: 2, type: 'step.started' },
			{ seq: 3, type: 'step.completed' }
		]
	});
	await runRowLoc.getByTestId('run-watch').click();
	await expect(root.getByTestId('watched-state')).toHaveText('Running', { timeout: 8000 });
	await waitForStreamRequests(1);
	expect(streamRequests[0]?.cursor).toBeNull();
	expect(streamRequests[0]?.path).toBe(`/api/v1/jobs/runs/${RUN_A}/events`);
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
	runScripts[RUN_A] = [
		{
			state: 'succeeded',
			updated_at: '2026-10-15T10:01:00Z',
			steps: [
				{ seq: 1, capability_id: 'host.shell', state: 'succeeded', receipt: null },
				{ seq: 2, capability_id: 'host.shell', state: 'succeeded', receipt: null }
			]
		}
	];
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
	await expect(runRowLoc.getByTestId('run-row-state')).toHaveText('Succeeded');
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
	const cancels = restMutations('POST', `/api/v1/jobs/runs/${RUN_A}/cancel`);
	expect(cancels).toHaveLength(1);
	expect(cancels[0]?.body).toEqual({ cancel_children: false });

	// Verified path: only the facade's KNOWN engine outcome may say
	// "cancelled" — here the armed answer reports it within the budget.
	const rowD = root.locator(`[data-testid="run-row"][data-run-id="${RUN_D}"]`);
	await rowD.getByTestId('run-watch').click();
	await expect(root.getByTestId('watched-id')).toHaveText(RUN_D.slice(0, 8), { timeout: 8000 });
	cancelOutcomeArmed = 'cancelled';
	await root.getByTestId('watched-cancel').click();
	await expect(root.getByTestId('cancel-verified')).toHaveText('Cancelled (verified)');
	await expect(root.getByTestId('watched-state')).toHaveText('Cancelled (verified)');
	await expect(rowD.getByTestId('run-row-state')).toHaveText('Cancelled (verified)');

	// Failure path: a refused cancel is an explicit value-free error; no
	// state is claimed anywhere. (Remount resets the panel; the durable rows
	// persist — RUN_A is still running with its recorded cancel request.)
	fails.cancel = {
		status: 409,
		code: 'conflict',
		message: 'the engine has no workflow under this run id'
	};
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

	// Nothing was submitted or cancelled by hiding, and NO mutation ever left
	// the page: a closing/hiding window starts and cancels nothing.
	expect(restBodies).toHaveLength(0);

	// Restore: a FRESH mount reloads the lists; the watched run resets to
	// none (no ghost subscription re-arms by itself).
	await apiCall(page, 'toggleHost', ['A']);
	await expect(root.getByTestId('run-none')).toBeVisible({ timeout: 8000 });
	await expect(root.getByTestId('run-row')).toHaveCount(3);
	await page.waitForTimeout(1200);
	expect(streamRequests).toHaveLength(1);
	expect(restBodies).toHaveLength(0);

	// Focus change (activating the sibling panel) starts and cancels nothing.
	await page.locator('[data-testid="sibling-panel"]').click();
	await page.waitForTimeout(300);
	expect(restBodies).toHaveLength(0);
	expect(streamRequests).toHaveLength(1);
});

// ---------------------------------------------------------------------------
// W3: 10-row pagination + filters, Today bounds, enriched rows, watch report
// ---------------------------------------------------------------------------

test.describe('W3 run list and watch report', () => {
	test.use({ timezoneId: 'Asia/Tokyo' });

	test('10-row pages with load-more dedup; q/date/tag filters ride the wire; Today emits exact local-midnight bounds', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
		addRuns(SERVER_A, 24, 'Nightly sweep');
		// The initial page already loaded with the 3 seeded runs; refresh to
		// page through the full 27-run store.
		await root.getByTestId('runs-refresh').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10);

		// Page 0 asked for exactly 10 rows under the explicit scope.
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10'
		});

		// Load more: the CURRENT offset rides the wire; run ids dedup and the
		// filtered total updates; the control disappears when drained.
		await root.getByTestId('runs-load-more').click();
		await expect(root.getByTestId('run-row')).toHaveCount(20, { timeout: 8000 });
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10',
			offset: '10'
		});
		const ids = await root.getByTestId('run-row').evaluateAll((rows) =>
			rows.map((row) => (row as HTMLElement).dataset.runId)
		);
		expect(new Set(ids).size).toBe(20);
		await expect(root.getByTestId('runs-total')).toHaveText('Showing 20 of 27 recorded runs.');
		await root.getByTestId('runs-load-more').click();
		await expect(root.getByTestId('run-row')).toHaveCount(27, { timeout: 8000 });
		await expect(root.getByTestId('runs-load-more')).toHaveCount(0);

		// q filter: applied to page 0 (offset RESET), replaces rows, total is
		// the FILTERED total.
		await root.getByTestId('runs-q-input').fill('nightly sweep 0');
		await root.getByTestId('runs-apply-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(9, { timeout: 8000 }); // 01..09
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10',
			q: 'nightly sweep 0'
		});
		await expect(root.getByTestId('runs-total')).toHaveText('Showing 9 of 9 recorded runs.');

		// Explicit date bounds ride the wire verbatim as ISO instants.
		await root.getByTestId('runs-q-input').fill('');
		await root.getByTestId('runs-date-from-input').fill('2026-10-15T09:00:00Z');
		await root.getByTestId('runs-date-to-input').fill('2026-10-15T09:59:59Z');
		await root.getByTestId('runs-apply-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(3, { timeout: 8000 }); // the three seeded runs
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10',
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
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10',
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
		runsStore[SERVER_A][RUN_A].state = 'failed';
		runsStore[SERVER_A][RUN_A].updated_at = '2026-10-15T09:07:00Z';
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
		expect(restCalls('GET', `/api/v1/servers/${SERVER_A}`)).toHaveLength(1);

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
		// Replay: MORE THAN 500 delta frames are delivered — every one is
		// skipped from the rendered collection ENTIRELY (no rows, no content,
		// no renaming) while the final round, the tool result and the failure
		// reason render usefully.
		const secretDelta = 'SECRET-DELTA-CONTENT-7f3a';
		const secretThinking = 'SECRET-THINKING-CONTENT-91bc';
		const frames: Array<{ seq: number; type: string; data?: unknown }> = [
			{ seq: 1, type: 'run.transition', data: {} } // one plain lifecycle row
		];
		for (let seq = 2; seq <= 520; seq += 1) {
			frames.push({
				seq,
				type: 'agent.output_delta',
				data: { output: secretDelta, thinking: secretThinking }
			});
		}
		frames.push(
			{ seq: 521, type: 'agent.model_round', data: { status: 'final', output: 'Deploy finished.\nAll 4 hosts green.' } },
			{
				seq: 522,
				type: 'agent.tool_result',
				data: { capability_id: 'host.shell', result: { exit_code: 0, stdout: 'ok' } }
			},
			{ seq: 523, type: 'agent.failed', data: { reason: 'verify step timed out after 30s' } }
		);
		queueStreamSession({ runId: RUN_A, frames });
		const finalText = root.getByTestId('watch-report-final');
		await expect(finalText).toHaveText('Deploy finished.\nAll 4 hosts green.', {
			timeout: 15000,
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
		expect(body.toLowerCase()).not.toContain('delta');
		// ZERO delta DOM rows: only the lifecycle, final-round, tool-result and
		// failed lifecycle lines remain (4 rows from 523 delivered frames).
		await expect(root.getByTestId('event-row')).toHaveCount(4);
		await expect(
			root.locator('[data-testid="event-row"]', { hasText: 'Run state changed' })
		).toHaveCount(1);
		await expect(
			root.locator('[data-testid="event-row"]', { hasText: 'Model round' })
		).toHaveCount(1);
		// Deltas still advanced the seq watermark: the reconnect cursor is the
		// LAST delivered seq (523), and the durable refresh was armed.
		await waitForStreamRequests(2);
		const cursors = streamRequests.map((request) => request.cursor);
		expect(cursors.at(-1)).toBe('523');
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

// ---------------------------------------------------------------------------
// HT-W6B: bounded tag previews + full lists, catalog any/all filters,
// assign/remove with final-tag honesty — all on the wire
// ---------------------------------------------------------------------------

test.describe('HT-W6B job tags', () => {
	test('rows show the bounded preview; the popup loads the full list on demand over the job tag route', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
		const rowA = root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"]`);
		const trigger = rowA.getByTestId('run-tags-trigger');

		// Bounded preview: first three names + the honest +9 (total 12).
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
		await expect(rowA.getByTestId('run-tags-chip')).toHaveText(['tag-00', 'tag-01', 'tag-02']);
		await expect(rowA.getByTestId('run-tags-more')).toHaveText('+9');
		await expect(rowA.getByTestId('run-tags-pop')).toBeHidden();

		// KEYBOARD: focus opens the popup; the first page rides the real job
		// tag route with the explicit same-server scope.
		await trigger.focus();
		await expect(trigger).toHaveAttribute('aria-expanded', 'true');
		const pop = rowA.getByTestId('run-tags-pop');
		await expect(pop).toBeVisible();
		await expect(pop.getByTestId('run-tags-loading')).toHaveCount(0);
		await expect(pop.getByTestId('run-tags-row')).toHaveCount(10);
		await expect(pop.getByTestId('run-tags-count')).toHaveText('Showing 10 of 12');
		const tagQuery = lastQuery('GET', `/api/v1/jobs/runs/${RUN_A}/tags`);
		expect(tagQuery.get('server_id')).toBe(SERVER_A);
		expect(tagQuery.get('limit')).toBe('10');
		expect(tagQuery.get('offset')).toBe('0');

		// Load more: offset 10 rides the wire; the fixture re-serves one stray
		// duplicate (tag-00) on the second page — dedup keeps an honest 12/12.
		await pop.getByTestId('run-tags-load-more').click();
		await expect(pop.getByTestId('run-tags-row')).toHaveCount(12, { timeout: 8000 });
		await expect(pop.getByTestId('run-tags-count')).toHaveText('Showing 12 of 12');
		await expect(pop.getByTestId('run-tags-load-more')).toHaveCount(0);
		const moreQuery = lastQuery('GET', `/api/v1/jobs/runs/${RUN_A}/tags`);
		expect(moreQuery.get('offset')).toBe('10');
		const rowNames = await pop.getByTestId('run-tags-row').evaluateAll((rows) =>
			rows.map((row) => row.getAttribute('title'))
		);
		expect(new Set(rowNames).size).toBe(12);

		// ESCAPE closes from inside the popup and restores trigger focus.
		await pop.getByTestId('run-tags-row').first().focus();
		await page.keyboard.press('Escape');
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
		await expect(pop).toBeHidden();
		await expect(trigger).toBeFocused();

		// HOVER opens on a real pointer move; a click on the already-open
		// (hover-opened) popup toggles it CLOSED — the accepted click-toggle
		// contract (mouse travel always hovers first, so click-to-open is not
		// a thing; tap-open is proven in the 360x640 test below).
		await trigger.hover();
		await expect(trigger).toHaveAttribute('aria-expanded', 'true');
		await expect(pop).toBeVisible();
		await trigger.click();
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
		await expect(pop).toBeHidden();

		// Scope honesty: host B never queries A's runs' tags; the recorded
		// tag reads all name SERVER_A's scope.
		const recorded = restCalls('GET', `/api/v1/jobs/runs/${RUN_A}/tags`);
		for (const entry of recorded) {
			expect(new URLSearchParams(entry.search).get('server_id')).toBe(SERVER_A);
		}
	});

	test('catalog-backed any/all filters are capped at 50, reset offset, compose with q/date, keep Load more, and clear', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
		seedTaggedSweeps();
		await root.getByTestId('runs-refresh').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10); // 15 runs total

		// Load one extra page FIRST: applying a filter must reset the offset.
		await root.getByTestId('runs-load-more').click();
		await expect(root.getByTestId('run-row')).toHaveCount(15, { timeout: 8000 });

		// Open the bounded catalog picker: the first page rides /tags.
		await root.getByTestId('runs-tag-picker-toggle').click();
		const picker = root.getByTestId('tag-filter-picker');
		await expect(picker).toBeVisible();
		await expect(picker.getByTestId('tag-filter-option')).toHaveCount(10);
		await expect(picker.getByTestId('tag-catalog-total')).toHaveText(
			'Showing 10 of 52 catalog tags.'
		);
		const catalogQuery = lastQuery('GET', '/api/v1/tags');
		expect(catalogQuery.get('limit')).toBe('10');
		expect(catalogQuery.get('offset')).toBe('0');

		// Bounded catalog Load more (no unbounded loading anywhere).
		await picker.getByTestId('tag-catalog-load-more').click();
		await expect(picker.getByTestId('tag-filter-option')).toHaveCount(20, { timeout: 8000 });
		expect(lastQuery('GET', '/api/v1/tags').get('offset')).toBe('10');

		// any-match (default): tag-01 OR tag-02 → RUN_A + 12 sweeps = 13 rows;
		// the query resets the offset and carries BOTH repeated tag ids.
		const option02 = picker.locator(`[data-testid="tag-filter-option"][data-tag-id="${TAG_02}"]`);
		const option03 = picker.locator(`[data-testid="tag-filter-option"][data-tag-id="${TAG_03}"]`);
		await option02.click();
		await option03.click();
		await expect(root.getByTestId('tag-filter-chip')).toHaveText(['tag-02 ×', 'tag-03 ×']);
		await root.getByTestId('runs-apply-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10, { timeout: 8000 });
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10',
			tag_id: [TAG_02, TAG_03],
			tag_match: 'any'
		});

		// Bounded Load more PRESERVED under the tag filter: offset 10 with the
		// same tag params; 14 matched rows (RUN_A, RUN_D and the 12 sweeps).
		await root.getByTestId('runs-load-more').click();
		await expect(root.getByTestId('run-row')).toHaveCount(14, { timeout: 8000 });
		await expect(root.getByTestId('runs-total')).toHaveText('Showing 14 of 14 recorded runs.');
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10',
			offset: '10',
			tag_id: [TAG_02, TAG_03],
			tag_match: 'any'
		});

		// all-match: BOTH tags on every row → only the 12 sweeps.
		await root.getByTestId('tag-match-select').selectOption('all');
		await root.getByTestId('runs-apply-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10, { timeout: 8000 });
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10',
			tag_id: [TAG_02, TAG_03],
			tag_match: 'all'
		});

		// Composes with q: the sweep labels + all-match stay on the wire.
		await root.getByTestId('runs-q-input').fill('Tagged sweep');
		await root.getByTestId('runs-apply-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10, { timeout: 8000 });
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10',
			q: 'Tagged sweep',
			tag_id: [TAG_02, TAG_03],
			tag_match: 'all'
		});

		// Composes with dates: the seeded 09:00 runs fall outside 10:00+.
		await root.getByTestId('runs-date-from-input').fill('2026-10-15T10:00:00Z');
		await root.getByTestId('runs-apply-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10, { timeout: 8000 });
		const composed = queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!);
		expect(composed).toMatchObject({
			server_id: SERVER_A,
			date_from: '2026-10-15T10:00:00Z',
			tag_match: 'all'
		});

		// Clear: no tag/q/date params ride the wire; rows return unfiltered.
		await root.getByTestId('runs-clear-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10, { timeout: 8000 });
		expect(queryShape(restCalls('GET', '/api/v1/jobs/runs').at(-1)!)).toEqual({
			server_id: SERVER_A,
			limit: '10'
		});
		await expect(root.getByTestId('tag-filter-chip')).toHaveCount(0);

		// The 50-cap: select 50 of the 52 catalog tags; the 51st option
		// disables with an honest cap note and the query carries EXACTLY 50.
		await root.getByTestId('runs-tag-picker-toggle').click();
		// The picker reopens with its already-adopted bounded pages; drain the
		// remaining pages (20 loaded → 30, 40, 50, 52) until Load more hides.
		for (const expected of [30, 40, 50, 52]) {
			await root.getByTestId('tag-catalog-load-more').click();
			await expect(root.getByTestId('tag-filter-option')).toHaveCount(expected, { timeout: 8000 });
		}
		await expect(root.getByTestId('tag-catalog-load-more')).toHaveCount(0);
		const options = root.getByTestId('tag-filter-option');
		for (let index = 0; index < 50; index += 1) {
			await options.nth(index).click();
		}
		await expect(root.getByTestId('tag-filter-cap')).toHaveText(
			'At most 50 tags can be matched — deselect others first.'
		);
		await expect(options.nth(50)).toBeDisabled();
		await root.getByTestId('runs-apply-filters').click();
		await expect(root.getByTestId('run-row')).toHaveCount(10, { timeout: 8000 });
		const capped = new URLSearchParams(restCalls('GET', '/api/v1/jobs/runs').at(-1)!.search);
		expect(capped.getAll('tag_id')).toHaveLength(50);
	});

	test('assign/remove on the watched run ride the job tag routes; final-tag and errors stay honest', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
		const rowC = root.locator(`[data-testid="run-row"][data-run-id="${RUN_C}"]`);
		await rowC.getByTestId('run-watch').click();
		await expect(root.getByTestId('watched-state')).toHaveText('Succeeded', { timeout: 8000 });

		// The watched surface shows the bounded preview + the association list.
		const watchTags = root.getByTestId('watch-tags-block');
		await expect(watchTags.getByTestId('watch-tags-trigger')).toHaveAttribute('aria-expanded', 'false');
		await expect(watchTags.getByTestId('watch-tags-chip')).toHaveText(['tag-00']);
		await expect(watchTags.getByTestId('watch-tags-more')).toHaveCount(0); // total 1, no +N
		await expect(watchTags.getByTestId('watch-tag-row')).toHaveCount(1);
		await expect(watchTags.getByTestId('watch-tag-name')).toHaveText('tag-00');
		await expect(watchTags.getByTestId('watch-tag-source')).toHaveText('system');
		await expect(watchTags.getByTestId('watch-tags-total')).toHaveText('Showing 1 of 1 attached tags.');
		// Wire evidence: the association page named the run + its server scope.
		const initialTagsQuery = lastQuery('GET', `/api/v1/jobs/runs/${RUN_C}/tags`);
		expect(initialTagsQuery.get('server_id')).toBe(SERVER_A);
		expect(initialTagsQuery.get('limit')).toBe('10');

		// ASSIGN: the bounded catalog feeds the picker; the POST body carries
		// the tag AND the explicit server scope; CSRF rides the mutation.
		await watchTags.getByTestId('watch-tag-catalog-load').click();
		await expect(watchTags.getByTestId('watch-tag-assign-select')).toBeVisible();
		expect(lastQuery('GET', '/api/v1/tags').get('limit')).toBe('10');
		// tag-00 is already attached; it must not be offered again.
		const options = watchTags.getByTestId('watch-tag-assign-select').locator('option');
		await expect(options).toHaveText([
			'Select a tag…',
			'tag-01',
			'tag-02',
			'tag-03',
			'tag-04',
			'tag-05',
			'tag-06',
			'tag-07',
			'tag-08',
			'tag-09'
		]);
		await watchTags.getByTestId('watch-tag-assign-select').selectOption({ label: 'tag-01' });
		await watchTags.getByTestId('watch-tag-assign').click();
		await expect(watchTags.getByTestId('watch-tag-row')).toHaveCount(2, { timeout: 8000 });
		const assigns = restMutations('POST', `/api/v1/jobs/runs/${RUN_C}/tags`);
		expect(assigns).toHaveLength(1);
		expect(assigns[0]?.body).toEqual({ tag_id: TAG_01, server_id: SERVER_A });
		expect(assigns[0]?.csrf).toBe('fixture-csrf-token');
		// Durable projections refreshed: the replayed detail + association page.
		await expect(watchTags.getByTestId('watch-tag-name')).toHaveText(['tag-00', 'tag-01']);
		await expect(rowC.getByTestId('run-tags-chip')).toHaveText(['tag-00', 'tag-01']);
		await expect(rowC.getByTestId('run-tags-more')).toHaveCount(0); // total 2 = preview 2, no +N
		const refreshDetail = restCalls('GET', `/api/v1/jobs/runs/${RUN_C}`).length;
		expect(refreshDetail).toBeGreaterThanOrEqual(2); // initial watch + projection refresh
		expect(restCalls('GET', `/api/v1/jobs/runs/${RUN_C}/tags`).length).toBeGreaterThanOrEqual(2);

		// REMOVE a non-final tag: idempotent DELETE with the explicit scope.
		const rowToRemove = watchTags
			.locator('[data-testid="watch-tag-row"]')
			.filter({ hasText: 'tag-01' });
		await rowToRemove.getByTestId('watch-tag-remove').click();
		await expect(watchTags.getByTestId('watch-tag-row')).toHaveCount(1, { timeout: 8000 });
		const removes = restMutations('DELETE', `/api/v1/jobs/runs/${RUN_C}/tags/${TAG_01}`);
		expect(removes).toHaveLength(1);
		const removeQuery = new URLSearchParams(removes[0]!.search);
		expect(removeQuery.get('server_id')).toBe(SERVER_A);
		expect(removeQuery.get('replacement_tag_id')).toBeNull();

		// FINAL TAG: removing the last tag is REFUSED (409 last_tag); the
		// refusal stays visible, the tag stays attached — no fabricated loss.
		await watchTags.getByTestId('watch-tag-remove').click();
		const refusalError = root.getByTestId('watch-tag-error');
		await expect(refusalError).toBeVisible({ timeout: 8000 });
		await expect(refusalError).toHaveAttribute('data-error-code', 'last_tag');
		await expect(refusalError).toContainText('final tag');
		await expect(watchTags.getByTestId('watch-tag-row')).toHaveCount(1);
		await expect(watchTags.getByTestId('watch-tag-name')).toHaveText('tag-00');
		await expect(watchTags.getByTestId('watch-tag-refusal')).toBeVisible();

		// Reviewed recovery: same-transaction replacement re-classification.
		await watchTags.getByTestId('watch-tag-replacement-select').selectOption({ label: 'tag-02' });
		await watchTags.getByTestId('watch-tag-replace-remove').click();
		await expect(watchTags.getByTestId('watch-tag-row')).toHaveCount(1, { timeout: 8000 });
		await expect(watchTags.getByTestId('watch-tag-name')).toHaveText('tag-02');
		await expect(root.getByTestId('watch-tag-error')).toHaveCount(0);
		await expect(watchTags.getByTestId('watch-tag-refusal')).toHaveCount(0);
		const swap = restMutations('DELETE', `/api/v1/jobs/runs/${RUN_C}/tags/${TAG_00}`).at(-1);
		expect(new URLSearchParams(swap!.search).get('replacement_tag_id')).toBe(TAG_02);
		await expect(rowC.getByTestId('run-tags-chip')).toHaveText(['tag-02']);

		// ERROR honesty: a refused removal changes nothing and says so.
		fails.removeRunTag = { status: 503, code: 'unavailable', message: 'tags facade unavailable' };
		await watchTags.getByTestId('watch-tag-remove').click();
		const actionError = root.getByTestId('watch-tag-error');
		await expect(actionError).toBeVisible({ timeout: 8000 });
		await expect(actionError).toHaveAttribute('data-error-code', 'unavailable');
		await expect(watchTags.getByTestId('watch-tag-row')).toHaveCount(1);
		await expect(watchTags.getByTestId('watch-tag-name')).toHaveText('tag-02');

		// ERROR honesty on assign: no optimistic addition.
		fails.assignRunTag = { status: 503, code: 'unavailable', message: 'tags facade unavailable' };
		await watchTags.getByTestId('watch-tag-assign-select').selectOption({ label: 'tag-04' });
		await watchTags.getByTestId('watch-tag-assign').click();
		await expect(root.getByTestId('watch-tag-error')).toHaveAttribute(
			'data-error-code',
			'unavailable',
			{ timeout: 8000 }
		);
		await expect(watchTags.getByTestId('watch-tag-row')).toHaveCount(1);
	});

	test('the watch surface keeps the report content alongside the current tags', async ({
		page
	}) => {
		const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
		await root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"] [data-testid="run-watch"]`).click();
		await expect(root.getByTestId('watched-state')).toHaveText('Running', { timeout: 8000 });
		const report = root.getByTestId('watch-report');
		await expect(report).toBeVisible();
		queueStreamSession({
			runId: RUN_A,
			frames: [
				{ seq: 1, type: 'run.submitted' },
				{ seq: 2, type: 'agent.model_round', data: { status: 'final', output: 'Migration applied to 3 shards.' } }
			]
		});
		await expect(root.getByTestId('watch-report-final')).toHaveText(
			'Migration applied to 3 shards.',
			{ timeout: 15000 }
		);
		// The current tags co-render with the untouched report content.
		const watchTags = root.getByTestId('watch-tags-block');
		await expect(watchTags.getByTestId('watch-tags-trigger')).toBeVisible();
		await expect(watchTags.getByTestId('watch-tags-chip')).toHaveText(['tag-00', 'tag-01', 'tag-02']);
		await expect(watchTags.getByTestId('watch-tags-more')).toHaveText('+9');
		await expect(root.getByTestId('watch-report-final')).toHaveText('Migration applied to 3 shards.');
		await expect(root.getByTestId('tool-result-list')).toHaveCount(0);
	});

	test.describe('360x640 touch context', () => {
		test.use({ hasTouch: true, viewport: { width: 360, height: 640 } });

		// Touch taps (not mouse clicks) are the open/close activation here: a
		// mouse click always travels over the trigger and hover-opens first,
		// so its own click toggles the popup closed (the accepted contract).
		test('360x640: row and watch tag surfaces stay inside the viewport with bounded popups', async ({
			page
		}) => {
			const root = page.locator(`${hostA} [data-testid="jobs-root"]`);
			const rowA = root.locator(`[data-testid="run-row"][data-run-id="${RUN_A}"]`);
			const trigger = rowA.getByTestId('run-tags-trigger');
			await trigger.tap();
			const pop = rowA.getByTestId('run-tags-pop');
			await expect(pop).toBeVisible({ timeout: 8000 });
			await expect(pop.getByTestId('run-tags-row')).toHaveCount(10);
			// No document horizontal overflow; the popup stays a bounded scroll.
			const overflow = await page.evaluate(() => ({
				scrollWidth: document.documentElement.scrollWidth,
				innerWidth: window.innerWidth
			}));
			expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.innerWidth);
			const box = await pop.boundingBox();
			expect(box).not.toBeNull();
			expect(box!.x).toBeGreaterThanOrEqual(0);
			expect(box!.x + box!.width).toBeLessThanOrEqual(360);
			const listScroll = await pop
				.getByTestId('run-tags-list')
				.evaluate((element) => ({
					scrollHeight: element.scrollHeight,
					clientHeight: element.clientHeight
				}));
			expect(listScroll.clientHeight).toBeLessThan(360);
			// Second tap toggles the open popup closed.
			await trigger.tap();
			await expect(pop).toBeHidden();

			// The watch surface's popup behaves the same at 360x640.
			await rowA.getByTestId('run-watch').tap();
			await expect(root.getByTestId('watched-state')).toHaveText('Running', { timeout: 8000 });
			const watchTrigger = root
				.getByTestId('watch-tags-block')
				.getByTestId('watch-tags-trigger');
			await watchTrigger.tap();
			const watchPop = root.getByTestId('watch-tags-pop');
			await expect(watchPop).toBeVisible({ timeout: 8000 });
			const watchOverflow = await page.evaluate(() => ({
				scrollWidth: document.documentElement.scrollWidth,
				innerWidth: window.innerWidth
			}));
			expect(watchOverflow.scrollWidth).toBeLessThanOrEqual(watchOverflow.innerWidth);
			await watchTrigger.tap();
			await expect(watchPop).toBeHidden();
		});
	});
});
