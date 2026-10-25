import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * act `21-outcome-ui` — real-browser proofs for the task outcome presentation
 * (`frontend/src/lib/panels/chat/TaskEvidence.svelte` composed by the REAL
 * `ChatPanel.svelte`).
 *
 * Focused per the test policy (mirrors the reviewed `chat.spec.ts` harness):
 * an in-memory Vite production build mounts the REAL panel through the
 * reviewed `PanelHost` + registry with a fixture in-page `ChatApi`; the run
 * summary's additive `task_outcome` document travels EXACTLY the wire shape
 * `mai.agents.task_completion.OutcomeObservation.document()` produces (the
 * backend producer suite pins that shape; this suite pins the consumer).
 *
 * Proven rows (act_RUNTIME E1, E2, C2, U1, U2):
 * - E1: a SUCCEEDED run whose outcome document is `partially_verified` renders
 *   "partially verified" and NEVER "fully verified"; missing checks stay
 *   visible and resumable.
 * - E2: a rejected completion proposal renders the stable rejection codes and
 *   never a verified verdict.
 * - C2: reopening the finished conversation preserves the server-delivered
 *   outcome and records NO mutation (no submit/steer/cancel/task write).
 * - U1: the criterion list pages at 10 with "Load more" inside a bounded
 *   scroll container at 360x640 — no horizontal overflow.
 * - U2: absent or malformed documents render NO block — never an invented
 *   verdict (C08 rule 6).
 */

const FIXTURE_URL = '/mai-task-evidence-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-task-evidence-app.svelte';
const ENTRY_ID = 'virtual:mai-task-evidence-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/ChatPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/06-web-chat.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const TASK_ID = 'f70a0000-1111-4222-8333-444455556666';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

type Scenario = 'outcome' | 'canonical';

type Fail = { status: number; code: string; message: string };

interface RecordedCall {
	op: string;
	runId?: string;
	serverId?: string;
	cursor?: string | null;
	payload?: unknown;
}

/** One value-free `task_outcome` document (wire shape of the final seam). */
interface WireOutcome {
	task_id: string | null;
	outcome: string | null;
	verified: boolean;
	recorded: boolean;
	criteria: { total: number; supported: number; failed: number; unavailable: number };
	criterion_states: Array<{ criterion_id: string; state: string }>;
	proposal: string;
	disposition?: string;
	revision?: number;
	rejection_code?: string;
	rejection_fields?: Record<string, string>;
}

interface TaskWindowApi {
	calls: RecordedCall[];
	streams: Array<{ runId: string; cursor: string | null }>;
	setConversationState(runId: string, state: string): void;
	completeRun(runId: string): void;
	lastCanonicalId(): string | null;
}

declare global {
	interface Window {
		__maiTaskApi?: TaskWindowApi;
		/** Boot scenario (harness-owned name, same convention as chat.spec). */
		__maiTaskScenario?: Scenario;
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
		const api = window.__maiTaskApi;
		return api ? api.calls.filter((call) => call.op === operation) : [];
	}, op);
}

// ---------------------------------------------------------------------------
// Fixture app (virtual module; runes mode like the reviewed harnesses)
// ---------------------------------------------------------------------------

const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry } from '${REGISTRY}';
	import { ApiError, subscribeEvents } from '${CLIENT}';
	import { chatEntry, chatServices } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const UUID_RE = ${String(UUID_RE)};

	const scenario = window.__maiTaskScenario ?? 'outcome';
	const calls = [];
	const streams = [];

	function clone(value) {
		return structuredClone(JSON.parse(JSON.stringify(value)));
	}

	function runRow(runId, serverId, state, overrides) {
		return Object.assign(
			{
				run_id: runId,
				server_id: serverId,
				parent_run_id: null,
				depth: 0,
				profile_revision: 3,
				prompt_revision: 1,
				model_profile: 'fixture-fast',
				requested_grants: ['fixture.read'],
				state,
				cancel_requested_at: null,
				submitted_at: '2026-10-16T09:00:00Z',
				updated_at: '2026-10-16T09:01:00Z'
			},
			overrides ?? {}
		);
	}

	const conversations = {};

	// --- in-page canonical conversation store (§3; the task-10 fixture) ------

	const canonical = window.__maiTaskScenario === 'canonical';
	const canonicalStore = {};
	let canonicalRevision = 1;
	let canonicalCounter = 0;

	function msgId() {
		canonicalCounter += 1;
		return 'e1000000-0000-4000-8000-' + String(canonicalCounter).padStart(12, '0');
	}

	function canonRow(conversationId, serverId) {
		return {
			conversation_id: conversationId,
			server_id: serverId,
			title: null,
			created_at: '2026-10-16T08:00:00Z',
			updated_at: '2026-10-16T08:30:00Z',
			message_count: 0,
			active_task_id: null,
			active_run_id: null,
			history_completeness: 'complete',
			missing_range: null,
			legacy_gaps: [],
			messages: [],
			events: [],
			latestSeq: 0
		};
	}

	function canonAppend(row, role, content) {
		row.latestSeq += 1;
		const message = {
			conversation_id: row.conversation_id,
			message_id: msgId(),
			sequence: row.messages.length + 1,
			role,
			source_kind: role === 'user' ? 'api' : 'system',
			created_at:
				'2026-10-16T08:' + String(10 + row.messages.length).padStart(2, '0') + ':00Z',
			content,
			redacted: false,
			content_ref: null
		};
		row.messages.push(message);
		row.message_count = row.messages.length;
		row.events.push({
			seq: row.latestSeq,
			kind: 'transcript.appended',
			message_id: message.message_id,
			role,
			source_kind: message.source_kind,
			redacted: message.redacted,
			has_content_ref: false,
			created_at: message.created_at
		});
		row.updated_at = message.created_at;
		return message;
	}

	const canonicalApi = {
		async listCanonicalConversations(serverId, page) {
			calls.push({ op: 'listCanonicalConversations', serverId, payload: JSON.parse(JSON.stringify(page ?? {})) });
			const rows = Object.values(canonicalStore)
				.filter((row) => row.server_id === serverId)
				.sort(
					(a, b) =>
						b.updated_at.localeCompare(a.updated_at) ||
						a.conversation_id.localeCompare(b.conversation_id)
				);
			const limit = page?.limit ?? 10;
			const offset = page?.offset ?? 0;
			const sliced = rows.slice(offset, offset + limit);
			return {
				limit,
				offset,
				total: rows.length,
				has_more: offset + sliced.length < rows.length,
				items: sliced.map((row) => ({
					conversation_id: row.conversation_id,
					server_id: row.server_id,
					title: row.title,
					created_at: row.created_at,
					updated_at: row.updated_at,
					message_count: row.message_count,
					active_task_id: row.active_task_id,
					active_run_id: row.active_run_id
				}))
			};
		},
		async getConversation(conversationId, serverId) {
			calls.push({ op: 'getConversation', runId: conversationId, serverId });
			const row = canonicalStore[String(conversationId)];
			if (!row || row.server_id !== serverId) {
				throw new ApiError(404, 'not_found', 'no conversation in the requesting server scope');
			}
			return {
				conversation_id: row.conversation_id,
				server_id: row.server_id,
				title: row.title,
				created_at: row.created_at,
				updated_at: row.updated_at,
				message_count: row.message_count,
				active_task_id: row.active_task_id,
				active_run_id: row.active_run_id,
				history_completeness: row.history_completeness,
				missing_range: null,
				legacy_gaps: []
			};
		},
		async listConversationMessages(conversationId, serverId, page) {
			calls.push({ op: 'listConversationMessages', runId: conversationId, serverId, payload: JSON.parse(JSON.stringify(page ?? {})) });
			const row = canonicalStore[String(conversationId)];
			if (!row || row.server_id !== serverId) {
				throw new ApiError(404, 'not_found', 'no conversation in the requesting server scope');
			}
			const limit = page?.limit ?? 10;
			const offset = page?.offset ?? 0;
			const ordered = [...row.messages].sort(
				(a, b) => a.sequence - b.sequence || a.message_id.localeCompare(b.message_id)
			);
			const sliced = ordered.slice(offset, offset + limit);
			return {
				limit,
				offset,
				total: ordered.length,
				has_more: offset + sliced.length < ordered.length,
				conversation_id: row.conversation_id,
				history_completeness: row.history_completeness,
				items: JSON.parse(JSON.stringify(sliced))
			};
		},
		async listConversationEvents(conversationId, serverId, query) {
			calls.push({ op: 'listConversationEvents', runId: conversationId, serverId, payload: JSON.parse(JSON.stringify(query ?? {})) });
			const row = canonicalStore[String(conversationId)];
			if (!row || row.server_id !== serverId) {
				throw new ApiError(404, 'not_found', 'no conversation in the requesting server scope');
			}
			const after = query?.afterSeq ?? 0;
			const limit = query?.limit ?? 50;
			const newer = row.events.filter((one) => one.seq > after);
			const sliced = newer.slice(0, limit);
			return {
				conversation_id: row.conversation_id,
				limit,
				offset: 0,
				total: newer.length,
				has_more: newer.length > sliced.length,
				after_seq: after,
				events: JSON.parse(JSON.stringify(sliced)),
				current: {
					latest_seq: row.latestSeq,
					history_completeness: row.history_completeness,
					active_run_id: row.active_run_id,
					active_task_id: row.active_task_id,
					pending_steering: 0
				}
			};
		},
		async createConversation(payload) {
			calls.push({ op: 'createConversation', payload: JSON.parse(JSON.stringify(payload)) });
			if (!UUID_RE.test(String(payload.client_request_id))) {
				throw new ApiError(422, 'validation_failed', 'client_request_id must be a canonical UUID string');
			}
			if (payload.server_id !== SERVER_A) {
				throw new ApiError(404, 'not_found', 'no canonical profile for the target server');
			}
			if (typeof payload.message !== 'string' || payload.message.length === 0) {
				throw new ApiError(422, 'validation_failed', 'message failed validation', '', { message: 'min_length' });
			}
			const id = String(payload.client_request_id);
			const row = canonRow(id, payload.server_id);
			const message = canonAppend(row, 'user', payload.message);
			canonicalStore[id] = row;
			conversations[id] = runRow(id, payload.server_id, 'running', { requested_grants: [] });
			row.active_run_id = id;
			return {
				conversation_id: id,
				message_id: message.message_id,
				task_id: null,
				run_id: id,
				revision: canonicalRevision,
				disposition: 'created'
			};
		},
		async submitConversationMessage(conversationId, payload) {
			calls.push({ op: 'submitConversationMessage', runId: conversationId, payload: JSON.parse(JSON.stringify(payload)) });
			const row = canonicalStore[String(conversationId)];
			if (!row || row.server_id !== payload.server_id) {
				throw new ApiError(404, 'not_found', 'no conversation in the requesting server scope');
			}
			const message = canonAppend(row, 'user', payload.message);
			const runId = String(payload.client_request_id);
			conversations[runId] = runRow(runId, payload.server_id, 'running', {});
			row.active_run_id = runId;
			return {
				conversation_id: row.conversation_id,
				message_id: message.message_id,
				task_id: null,
				run_id: runId,
				revision: canonicalRevision,
				disposition: 'created'
			};
		},
		async steerConversation(conversationId, payload) {
			calls.push({ op: 'steerConversation', runId: conversationId, payload: JSON.parse(JSON.stringify(payload)) });
			const row = canonicalStore[String(conversationId)];
			if (!row || row.server_id !== payload.server_id || row.active_run_id === null) {
				throw new ApiError(409, 'no_active_run', "no active run holds this conversation's writer lease", '', {
					conversation_id: 'no_active_run'
				});
			}
			return {
				conversation_id: row.conversation_id,
				steering_id: String(payload.client_request_id),
				run_id: row.active_run_id,
				action: payload.action,
				client_request_id: String(payload.client_request_id),
				disposition: 'created',
				delivery_status: 'pending'
			};
		}
	};

	const api = {
		// The canonical slice rides ONLY when the scenario mounted the store.
		...(canonical ? canonicalApi : {}),
		async submitMessage(payload) {
			calls.push({ op: 'submitMessage', payload: JSON.parse(JSON.stringify(payload)) });
			if (!UUID_RE.test(String(payload.run_id))) {
				throw new ApiError(422, 'validation_failed', 'run_id must be a canonical UUID string');
			}
			if (payload.server_id !== SERVER_A) {
				throw new ApiError(404, 'not_found', 'no canonical profile for the target server');
			}
			if (typeof payload.message !== 'string' || payload.message.length === 0) {
				throw new ApiError(422, 'validation_failed', 'message failed validation', '', { message: 'min_length' });
			}
			const conversation = runRow(String(payload.run_id), String(payload.server_id), 'running', {
				children: [],
				attachments: []
			});
			conversations[String(payload.run_id)] = conversation;
			return { conversation: clone(conversation), deduplicated: false };
		},
		async listConversations(serverId, page) {
			calls.push({ op: 'listConversations', serverId, payload: page ?? {} });
			const rows = Object.values(conversations)
				.filter((row) => row.server_id === serverId)
				.sort((a, b) => a.submitted_at.localeCompare(b.submitted_at));
			const limit = page?.limit ?? 10;
			const offset = page?.offset ?? 0;
			const sliced = rows.slice(offset, offset + limit);
			return { conversations: clone(sliced), limit, offset, total: rows.length };
		},
		async readConversation(runId) {
			calls.push({ op: 'readConversation', runId });
			const row = conversations[runId];
			if (!row) throw new ApiError(404, 'not_found', 'no run stored for this run id');
			return clone(row);
		},
		async cancelConversation(runId, payload) {
			calls.push({ op: 'cancelConversation', runId, payload: JSON.parse(JSON.stringify(payload)) });
			if (!conversations[runId]) throw new ApiError(404, 'not_found', 'no run stored for this run id');
			if (payload.cancel_children !== true) {
				throw new ApiError(422, 'validation_failed', 'cancel_children must be true');
			}
			return {
				run_id: runId,
				state: conversations[runId].state,
				outcome: null,
				requested_at: '2026-10-16T09:05:00Z',
				outcome_at: null,
				requested_now: true,
				unresolved_seqs: []
			};
		},
		async streamConversationEvents(runId, options) {
			calls.push({ op: 'streamConversationEvents', runId, cursor: options.cursor ?? null });
			streams.push({ runId, cursor: options.cursor ?? null });
			return subscribeEvents(
				'/api/v1/chat/runs/' + encodeURIComponent(runId) + '/events',
				options
			);
		},
		async readRun(runId) {
			calls.push({ op: 'readRun', runId });
			const row = conversations[runId];
			if (!row) throw new ApiError(404, 'not_found', 'no run stored for this run id');
			return Object.assign(clone(row), { steps: [] });
		},
		async readApproval(approvalId) {
			calls.push({ op: 'readApproval', runId: approvalId });
			throw new ApiError(404, 'not_found', 'no approval record for this id');
		},
		async decideApproval(approvalId, payload) {
			calls.push({ op: 'decideApproval', runId: approvalId, payload: JSON.parse(JSON.stringify(payload)) });
			throw new ApiError(404, 'not_found', 'no approval record for this id');
		}
	};

	const services = {
		...chatServices(api),
		live: false,
		serverOptions: [{ id: SERVER_A, label: 'Server Alpha' }],
		availableServices: []
	};

	const registryResult = createPanelRegistry([chatEntry()]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;

	window.__maiTaskApi = {
		calls,
		streams,
		setConversationState(runId, state) {
			if (!conversations[runId]) throw new Error('no such fixture conversation');
			conversations[runId].state = state;
			conversations[runId].updated_at = '2026-10-16T09:07:00Z';
		},
		completeRun(runId) {
			const run = conversations[String(runId)];
			if (run === undefined) throw new Error('no such fixture run');
			run.state = 'succeeded';
			const row = canonicalStore[String(runId)];
			if (row !== undefined && row.active_run_id === String(runId)) row.active_run_id = null;
		},
		lastCanonicalId() {
			return Object.keys(canonicalStore).length === 0 ? null : Object.keys(canonicalStore)[0];
		}
	};
</script>

<section data-testid="host-main" aria-label="Host main">
	<ComponentHost label="Host main">
		<PanelHost
			{registry}
			descriptor={{
				schema_version: 1,
				window_id: '6a6b7c8d-9e0f-4a1b-8c3d-4e5f6a7b8c9d',
				panel_kind: 'chat',
				scope: { kind: 'server', server_id: '${SERVER_A}' },
				geometry: { x: 8, y: 8, width: 780, height: 620 },
				presentation: 'normal',
				z: 1,
				title_override: null
			}}
			services={services}
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

interface StreamFrame {
	seq: number;
	type: string;
	data?: Record<string, unknown>;
}

interface StreamSession {
	frames?: StreamFrame[];
	abort?: boolean;
}

let streamSessions: StreamSession[] = [];
let streamRequests: Array<{ runId: string; cursor: string | null; path: string }> = [];
let heldStreamRoutes: Array<{ route: Route; runId: string; cursor: string | null }> = [];

function frame(seq: number, type: string, data: Record<string, unknown>): StreamFrame {
	return { seq, type, data };
}

function sseBody(runId: string, frames: StreamFrame[], cursor: string | null): string {
	const after = frames.filter((one) => cursor === null || one.seq > Number(cursor));
	return after
		.map(
			(one) =>
				`id: ${one.seq}\n` +
				`data: ${JSON.stringify({
					seq: one.seq,
					run_id: runId,
					ts: '2026-10-16T10:00:00Z',
					type: one.type,
					v: 1,
					data: one.data ?? {}
				})}\n\n`
		)
		.join('');
}

function fulfillStream(
	route: Route,
	session: StreamSession,
	runId: string,
	cursor: string | null
): Promise<void> {
	return route.fulfill({
		status: 200,
		contentType: 'text/event-stream',
		headers: { 'cache-control': 'no-store' },
		body: sseBody(runId, session.frames ?? [], cursor)
	});
}

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
		else await fulfillStream(route, session, match[1], cursor);
		return;
	}
	heldStreamRoutes.push({ route, runId: match[1], cursor });
}

/**
 * The panel fold AND the composed delegation tree both subscribe the root
 * run's stream (each with its own watermark) — sessions queue in PAIRS.
 */
function queueStreamPair(session: StreamSession): void {
	for (let i = 0; i < 2; i++) {
		streamSessions.push({ frames: session.frames, abort: session.abort });
	}
	const held = heldStreamRoutes.shift();
	if (!held) return;
	const next = streamSessions.shift();
	if (!next) {
		heldStreamRoutes.unshift(held);
		return;
	}
	void (next.abort ? held.route.abort() : fulfillStream(held.route, next, held.runId, held.cursor)).catch(
		() => {
			// The recorded requests are the source of truth.
		}
	);
}

async function waitForStreamRequests(count: number): Promise<void> {
	await expect.poll(() => streamRequests.length, { timeout: 15000 }).toBeGreaterThanOrEqual(count);
}

// ---------------------------------------------------------------------------

function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-task-evidence-harness',
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
			outDir: 'node_modules/.mai-task-evidence-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'task-evidence-[hash].js',
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
	if (chunks.length === 0) {
		throw new Error('harness bundle incomplete: the panel must build as a lazy chunk');
	}
});

test.beforeEach(async ({ page }) => {
	streamSessions = [];
	streamRequests = [];
	heldStreamRoutes = [];

	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAI task evidence fixture</title>
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
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: chunk.code });
		}
		return route.abort();
	});
	await page.route('**/api/v1/chat/runs/*/events', (route) => handleEventStream(route));
	page.on('pageerror', (error) => {
		throw new Error(`fixture page error: ${error.message}`);
	});
});

async function openFixture(page: Page, scenario: Scenario): Promise<Page> {
	await page.clock.setFixedTime(new Date('2026-10-16T09:30:00Z'));
	await page.addInitScript((value) => {
		window.__maiTaskScenario = value;
	}, scenario);
	await page.goto(FIXTURE_URL);
	await expect(page.getByTestId('host-main').getByTestId('chat-root')).toBeVisible();
	return page;
}

/** Fill the compose form and submit; waits for the recorded call. */
async function submitFromHost(page: Page, message: string): Promise<void> {
	const main = page.getByTestId('host-main');
	await main.getByTestId('compose-message').fill(message);
	await main.getByTestId('compose-profile').fill('fixture-fast');
	await main.getByTestId('chat-submit').click();
	await expect
		.poll(async () => (await callsOf(page, 'submitMessage')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(1);
}

async function submittedRunId(page: Page): Promise<string> {
	const submissions = await callsOf(page, 'submitMessage');
	const payload = submissions[submissions.length - 1].payload as Record<string, unknown>;
	return String(payload.run_id);
}

// --- wire documents (the exact value-free final-seam shape) ----------------

/** E1: one passed, one failed, one unavailable — verification NOT complete. */
const PARTIAL_OUTCOME: WireOutcome = {
	task_id: TASK_ID,
	outcome: 'partially_verified',
	verified: false,
	recorded: false,
	criteria: { total: 3, supported: 1, failed: 1, unavailable: 1 },
	criterion_states: [
		{ criterion_id: 'service_restart', state: 'passed' },
		{ criterion_id: 'health_check_green', state: 'unavailable' },
		{ criterion_id: 'log_error_scan', state: 'failed' }
	],
	proposal: 'none'
};

/** E2: the completion proposal was rejected — nothing recorded, all open. */
const REJECTED_OUTCOME: WireOutcome = {
	task_id: TASK_ID,
	outcome: 'in_progress',
	verified: false,
	recorded: false,
	criteria: { total: 2, supported: 0, failed: 0, unavailable: 2 },
	criterion_states: [
		{ criterion_id: 'build_done', state: 'unavailable' },
		{ criterion_id: 'smoke_test_green', state: 'unavailable' }
	],
	proposal: 'rejected',
	rejection_code: 'completion_missing_evidence',
	rejection_fields: { 'criterion_results[0]': 'missing_evidence_refs' }
};

/** U1: twelve criteria — pages at the bounded 10-per-page limit. */
function twelveCriterionOutcome(): WireOutcome {
	const states: Array<{ criterion_id: string; state: string }> = [];
	for (let i = 1; i <= 12; i++) {
		states.push({
			criterion_id: 'check_' + String(i).padStart(2, '0'),
			state: i <= 2 ? 'passed' : i <= 6 ? 'failed' : 'unavailable'
		});
	}
	return {
		task_id: TASK_ID,
		outcome: 'in_progress',
		verified: false,
		recorded: false,
		criteria: { total: 12, supported: 2, failed: 4, unavailable: 6 },
		criterion_states: states,
		proposal: 'none'
	};
}

function completedFrame(outcome: WireOutcome | null, seq = 1): StreamFrame {
	const data: Record<string, unknown> = { rounds: 1, tool_calls: 1, output_chars: 42 };
	if (outcome !== null) data['task_outcome'] = outcome;
	return frame(seq, 'agent.completed', data);
}

test('E1: a succeeded run with incomplete verification renders partially verified and never fully verified', async ({
	page
}) => {
	await openFixture(page, 'outcome');
	const main = page.getByTestId('host-main');
	await submitFromHost(page, 'Verify the fixture deployment');
	const runId = await submittedRunId(page);

	// The run SUCCEEDS technically while verification stays incomplete (E1).
	await page.evaluate((id) => window.__maiTaskApi!.completeRun(id), runId);
	await queueStreamPair({ frames: [completedFrame(PARTIAL_OUTCOME)] });
	await waitForStreamRequests(2);

	await expect(main.getByTestId('conversation-state')).toHaveText('Succeeded');
	const block = main.getByTestId('task-outcome');
	await expect(block).toBeVisible();
	// The verdict is driven by the DOCUMENT, not by the run's success state.
	await expect(block.getByTestId('task-outcome-verdict')).toHaveText('partially verified');
	await expect(block).not.toContainText('fully verified');
	await expect(block.getByTestId('task-outcome-counts')).toHaveText(
		'1 supported · 1 failed · 1 unavailable · 3 total'
	);
	// The missing checks stay visible and resumable (outstanding work).
	await expect(block.getByTestId('task-outcome-missing')).toContainText(
		'Missing checks: 2 of 3 criterion(s) outstanding'
	);
	const rows = block.getByTestId('task-outcome-criterion');
	await expect(rows).toHaveCount(3);
	await expect(rows.nth(0)).toContainText('Criterion service_restart');
	await expect(rows.nth(0)).toContainText('supported');
	await expect(rows.nth(1)).toContainText('Criterion health_check_green');
	await expect(rows.nth(1)).toContainText('unavailable');
	await expect(rows.nth(2)).toContainText('Criterion log_error_scan');
	await expect(rows.nth(2)).toContainText('failed');
	await expect(block.getByTestId('task-outcome-meta')).toHaveText('Showing 3 of 3 criteria.');
});

test('E2: a rejected completion proposal renders the stable rejection and never a verified outcome', async ({
	page
}) => {
	await openFixture(page, 'outcome');
	const main = page.getByTestId('host-main');
	await submitFromHost(page, 'Finish the fixture rollout');
	const runId = await submittedRunId(page);

	await page.evaluate((id) => window.__maiTaskApi!.completeRun(id), runId);
	await queueStreamPair({ frames: [completedFrame(REJECTED_OUTCOME)] });
	await waitForStreamRequests(2);

	const block = main.getByTestId('task-outcome');
	await expect(block).toBeVisible();
	await expect(block.getByTestId('task-outcome-verdict')).toHaveText('in progress');
	await expect(block).not.toContainText('fully verified');
	await expect(block.getByTestId('task-outcome-proposal')).toContainText(
		'Completion proposal rejected (completion_missing_evidence)'
	);
	await expect(block.getByTestId('task-outcome-rejection-fields')).toContainText(
		'criterion_results[0]: missing_evidence_refs'
	);
	await expect(block.getByTestId('task-outcome-missing')).toContainText(
		'Missing checks: 2 of 2 criterion(s) outstanding'
	);
});

test('U1: a 12-criterion outcome pages at 10 with Load more inside a bounded container at 360x640', async ({
	page
}) => {
	await page.setViewportSize({ width: 360, height: 640 });
	await openFixture(page, 'outcome');
	const main = page.getByTestId('host-main');
	await submitFromHost(page, 'Run every fixture check');
	const runId = await submittedRunId(page);

	await page.evaluate((id) => window.__maiTaskApi!.completeRun(id), runId);
	await queueStreamPair({ frames: [completedFrame(twelveCriterionOutcome())] });
	await waitForStreamRequests(2);

	const block = main.getByTestId('task-outcome');
	await expect(block).toBeVisible();
	const list = block.getByTestId('task-outcome-criteria');
	// Bounded scroll container (AGENTS.md §1): the list scrolls, never the page.
	await expect(list).toHaveCSS('overflow-y', 'auto');
	await expect(block.getByTestId('task-outcome-criterion')).toHaveCount(10);
	const overflow = await block.evaluate(
		(el) => el.scrollWidth > el.clientWidth + 1 // tolerance: subpixel rounding
	);
	expect(overflow).toBe(false);

	const loadMore = block.getByTestId('task-outcome-load-more');
	await expect(loadMore).toContainText('Load more criteria (10 of 12)');
	await expect(block.getByTestId('task-outcome-meta')).toHaveText('Showing 10 of 12 criteria.');
	await loadMore.click();
	await expect(block.getByTestId('task-outcome-criterion')).toHaveCount(12);
	await expect(block.getByTestId('task-outcome-meta')).toHaveText('Showing 12 of 12 criteria.');
	await expect(loadMore).toHaveCount(0);
});

test('U2: no task_outcome key and a malformed document render NO outcome block', async ({
	page
}) => {
	await openFixture(page, 'outcome');
	const main = page.getByTestId('host-main');
	await submitFromHost(page, 'Answer without a task');
	const runId = await submittedRunId(page);

	// (a) Taskless run: the key is absent — legacy surfaces stay untouched.
	await page.evaluate((id) => window.__maiTaskApi!.completeRun(id), runId);
	await queueStreamPair({ frames: [completedFrame(null)] });
	await waitForStreamRequests(2);
	await expect(main.getByTestId('conversation-state')).toHaveText('Succeeded');
	await expect(main.getByTestId('task-outcome')).toHaveCount(0);

	// (b) Malformed document (unknown outcome word, non-array states): the
	// fail-closed narrow renders nothing rather than a guessed verdict.
	await submitFromHost(page, 'Run a broken summary');
	const brokenRunId = await submittedRunId(page);
	await page.evaluate((id) => window.__maiTaskApi!.completeRun(id), brokenRunId);
	await queueStreamPair({
		frames: [
			frame(1, 'agent.completed', {
				rounds: 1,
				tool_calls: 0,
				output_chars: 4,
				task_outcome: {
					task_id: TASK_ID,
					outcome: 'totally_verified',
					verified: true,
					recorded: true,
					criteria: { total: 1 },
					criterion_states: 'all-done',
					proposal: 'accepted'
				}
			})
		]
	});
	await waitForStreamRequests(4);
	await expect(main.getByTestId('task-outcome')).toHaveCount(0);
	await expect(main).not.toContainText('fully verified');
});

test('C2: reopening the finished chat preserves the recorded outcome and records no mutation', async ({
	page
}) => {
	await openFixture(page, 'canonical');
	const main = page.getByTestId('host-main');

	await main.getByTestId('compose-message').fill('Check the fixture deployment');
	await main.getByTestId('compose-profile').fill('fixture-fast');
	await main.getByTestId('chat-submit').click();
	await expect
		.poll(async () => (await callsOf(page, 'createConversation')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(1);
	const conversationId = await page.evaluate(() => window.__maiTaskApi!.lastCanonicalId());
	expect(conversationId).not.toBeNull();

	// The run binds and finishes with the partial outcome document.
	await page.evaluate((id) => window.__maiTaskApi!.completeRun(id as string), conversationId);
	await queueStreamPair({ frames: [completedFrame(PARTIAL_OUTCOME)] });
	const block = main.getByTestId('task-outcome');
	await expect(block).toBeVisible();
	await expect(block.getByTestId('task-outcome-verdict')).toHaveText('partially verified');

	// Close and reopen from the recorded history (server truth, no cache).
	await main.getByTestId('chat-history-toggle').click();
	await expect(main.getByTestId('history-row')).toHaveCount(1);
	await main.getByTestId('chat-history-item').first().click();
	await expect(main.getByTestId('canonical-user-message')).toContainText(
		'Check the fixture deployment'
	);

	// The outcome survives the reopen — the SAME server-delivered document.
	await expect(main.getByTestId('task-outcome')).toBeVisible();
	await expect(main.getByTestId('task-outcome-verdict')).toHaveText('partially verified');
	await expect(main.getByTestId('task-outcome-counts')).toHaveText(
		'1 supported · 1 failed · 1 unavailable · 3 total'
	);

	// NO fake mutation: opening a finished chat only READS — no submit, no
	// steering, no cancel, no second task/plan write of any kind.
	expect(await callsOf(page, 'createConversation')).toHaveLength(1);
	expect(await callsOf(page, 'submitConversationMessage')).toHaveLength(0);
	expect(await callsOf(page, 'submitMessage')).toHaveLength(0);
	expect(await callsOf(page, 'steerConversation')).toHaveLength(0);
	expect(await callsOf(page, 'cancelConversation')).toHaveLength(0);
});
