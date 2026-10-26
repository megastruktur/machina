import { expect, test, type Locator, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * mai-06-web-chat — real-browser proofs for the conversation panel
 * (frontend/src/lib/panels/ChatPanel.svelte) and its feature-owned registry
 * entry (frontend/src/lib/workspace/entries/06-web-chat.ts).
 *
 * Focused per the test policy: the panel is exercised in real Chromium,
 * mounted through the reviewed window host (`PanelHost`) and registry with a
 * fixture in-page API implementing the panel's typed `ChatApi` shape (the
 * same interface `createMaiApiClient()` gains at desktop composition,
 * mirroring the reviewed `mai.api.chat` routes 1:1). The REAL backend
 * boundary — C01 envelope codes, session/origin/CSRF, idempotent submit
 * persistence, attachment integrity, budget ledger persistence — is owned by
 * the `06-chat-api`/`06-delegation-budgets` producer suites; the assembled
 * desktop journey (real Chat panel + fixture SSH + live model round) is
 * owned by `06-verify` at the phase gate.
 *
 * Harness (self-contained, following the models/delegation convention):
 *  1. test.beforeAll runs an IN-MEMORY Vite production build (write: false)
 *     of a virtual app importing the REAL panel, the REAL feature entry, the
 *     REAL registry + PanelHost, the REAL DelegationTree and the REAL client
 *     primitive. Five hosts mount: MAIN (server Alpha) + SECOND (server
 *     Beta — a DIFFERENT pinned scope) with the same services bag, BARE with
 *     no services (fail-closed), GLOBAL (a server-only entry must be refused)
 *     and one benign sibling panel (coexistence).
 *  2. Each test serves the built JS/CSS/HTML through Playwright route
 *     interception. The bag's `streamConversationEvents` is the REAL
 *     `subscribeEvents` over the reviewed chat producer URL, answered by
 *     route handlers — the concrete URL, `Last-Event-ID` cursor carry, frame
 *     parsing and reconnect loop are exercised on the wire, not stubbed. The
 *     fixture API mirrors the reviewed producer semantics: value-free rows
 *     and events, the explicit tree-cancel body, one-use approval decisions
 *     bound field-for-field.
 *
 * Both the panel AND the composed DelegationTree subscribe the root run's
 * stream (each with its own watermark), so stream sessions are queued in
 * PAIRS: one per live subscriber. Every subscriber deduplicates by its own
 * `seq` watermark — that is exactly the property the reconnect test proves.
 */

const FIXTURE_URL = '/mai-chat-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-chat-app.svelte';
const ENTRY_ID = 'virtual:mai-chat-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/ChatPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/06-web-chat.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
/** The General Consult sentinel (the wire's server_id default). */
const GLOBAL_CONSULT_SERVER_ID = '00000000-0000-0000-0000-000000000000';
const ROOT = 'aa111111-1111-4222-8333-444455556666';
const CHILD_A = 'bb222222-1111-4222-8333-444455556666';
const CHILD_B = 'cc333333-1111-4222-8333-444455556666';
const HISTORY_RUN = 'ee555555-1111-4222-8333-444455556666';
/** A recorded GLOBAL (General Consult) canonical conversation (24a D-1). */
const GLOBAL_CONV = 'dd666666-1111-4222-8333-444455556666';
const APPROVAL_ID = 'f1000000-1111-4222-8333-444455556666';
const OP_ID = 'ef000000-0000-4000-8000-000000000009';
const ATTACHMENT_SHA = 'cd'.repeat(32);
const PAYLOAD_SHA = 'ab'.repeat(32);

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Secret-SHAPED fixture canary (synthetic, NOT a secret): stands for the
 * conversation/context/tool bodies the value-free wire never carries. It is
 * planted in the fixture store as an UNKNOWN row field and must NEVER appear
 * in the DOM (C05 rule 4).
 */
const FIXTURE_CANARY = 'MAI-FIXTURE-CANARY-context-body-NEVER-DISPLAY';
/**
 * Hostile-shaped strings the fixture stream delivers as recorded VALUE-FREE
 * text (a stable reason and a classified failure reason). The panel must
 * render them as inert text — no element injection, no script execution.
 */
const HOSTILE_TOOL_TEXT = '<img src=x onerror="window.__maiPwned=1">MAI-FIXTURE-HOSTILE-TOOL-TEXT';
const HOSTILE_FAILURE = '<script>window.__maiPwned=2</script>fixture_denial';

type Scenario = 'compose' | 'replay' | 'history' | 'approval' | 'approval-absent' | 'canonical';

type Fail = { status: number; code: string; message: string };

interface RecordedCall {
	op: string;
	runId?: string;
	serverId?: string;
	cursor?: string | null;
	payload?: unknown;
}

interface ChatWindowApi {
	calls: RecordedCall[];
	streams: Array<{ runId: string; cursor: string | null }>;
	submissions: Array<{ payload: Record<string, unknown>; childIds: string[] }>;
	setConversationState(runId: string, state: string): void;
	setApprovalFields(fields: Record<string, unknown>): void;
	failApprovalRead(failure: Fail | null): void;
	armCancel(outcome: string | null): void;
	failNextRead(failure: Fail | null): void;
	armDecideFailure(failure: Fail | null): void;
	toggleMain(on: boolean): void;
	toggleSecond(on: boolean): void;
	seedConversations(serverId: string, count: number): void;
	/** Canonical fixture controls (scenario 'canonical'): arm the task
	 * revision the panel must pin with `expected_revision` (C6). */
	armRevision(next: number): void;
	/** Arm the writer-lease busy path: follow-ups return `queued_followup`. */
	armBusy(on: boolean): void;
	/** Append an assistant transcript row (the trusted producer's reply). */
	appendAssistant(conversationId: string, content: string): void;
	/** Mark the conversation's active run succeeded and release its lease. */
	completeRun(runId: string): void;
	/** Most recent test-authored canonical conversation id (or null). */
	lastCanonicalId(): string | null;
	/** Cross-server probe: returns ONLY the structured code (C8). */
	probeCrossServer(): Promise<{ code: string }>;
	/** Cross-server TASK probe: returns ONLY the structured code (24b D-2, C8). */
	probeCrossServerTask(): Promise<{ code: string }>;
	/** Extra metadata-only canonical rows for list pagination. */
	seedCanonicalConversations(count: number): void;
	/** Seed one complete canonical row on ANY recorded scope (24a: the
	 * Global-sentinel draft/restore journeys). */
	seedCanonicalRow(serverId: string, conversationId: string, title: string): void;
	/** One-shot: the NEXT canonical list resolves only after this delay
	 * (stale-response scope-guard proof, 24a D-1 rule 4). */
	armSlowList(delayMs: number): void;
	registry: {
		kinds: string[];
		checkScope(kind: string, scope: unknown): { ok: true } | { ok: false; code: string };
		defaultGeometry(kind: string): unknown;
		minGeometry(kind: string): unknown;
	};
}

declare global {
	interface Window {
		__maiChatApi?: ChatWindowApi;
		/** Set by a test init script: the boot scenario (chat-spec-owned name). */
		__maiChatScenario?: Scenario;
		__maiChatFramed?: boolean;
		/** Set by a test init script: mount the bag as a LIVE slice (the
		 * ModelProfileSelect may fetch the configured profiles). */
		__maiChatLive?: boolean;
		/** Set ONLY by hostile-content execution (its presence is the failure). */
		__maiPwned?: number;
		/** Set by a test init script: the in-page canonical conversation
		 * store mounts (scenario 'canonical' — the §3 surface is exercised
		 * with the panel's canonical-first behavior). */
		__maiChatCanonical?: boolean;
		/** Set by a test init script: the §8 task slice rides the bag (24b
		 * D-2 — the same slice the composed desktop binds from the real
		 * client). Without it the panel must fail closed honestly. */
		__maiChatTasks?: boolean;
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

/** Recorded fixture calls of one op (serialized VALUES only — fixture
 * methods never cross the evaluate boundary). */
async function callsOf(page: Page, op: string): Promise<RecordedCall[]> {
	return page.evaluate((operation) => {
		const api = window.__maiChatApi;
		return api ? api.calls.filter((call) => call.op === operation) : [];
	}, op);
}

// ---------------------------------------------------------------------------
// Fixture app (virtual module; compiled in RUNES mode like the reviewed
// harnesses, so it uses `$state` only)
// ---------------------------------------------------------------------------

const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import WindowFrame from '${FRONTEND_ROOT}src/lib/workspace/WindowFrame.svelte';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry } from '${REGISTRY}';
	import { createRawSnippet } from 'svelte';
	import { ApiError, subscribeEvents } from '${CLIENT}';
	import { chatEntry, chatServices } from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const ROOT = '${ROOT}';
	const CHILD_A = '${CHILD_A}';
	const CHILD_B = '${CHILD_B}';
	const HISTORY_RUN = '${HISTORY_RUN}';
	const APPROVAL_ID = '${APPROVAL_ID}';
	const OP_ID = '${OP_ID}';
	const GLOBAL_CONSULT = '${GLOBAL_CONSULT_SERVER_ID}';
	const ATTACHMENT_SHA = '${ATTACHMENT_SHA}';
	const PAYLOAD_SHA = '${PAYLOAD_SHA}';
	const CANARY = '${FIXTURE_CANARY}';
	const UUID_RE = ${String(UUID_RE)};

	const scenario = window.__maiChatScenario ?? 'compose';
	const calls = [];
	const streams = [];
	const submissions = [];
	let childCounter = 0;

	function clone(value) {
		return structuredClone(JSON.parse(JSON.stringify(value)));
	}

	function childId() {
		childCounter += 1;
		return 'd0000000-0000-4000-8000-' + String(childCounter).padStart(12, '0');
	}

	function consumeReadFailure() {
		if (failNextRead) {
			const failure = failNextRead;
			failNextRead = null;
			throw new ApiError(failure.status, failure.code, failure.message);
		}
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

	function childRow(runId, parentRunId, state, serverId) {
		return runRow(runId, serverId ?? SERVER_A, state, {
			parent_run_id: parentRunId,
			depth: 1,
			model_profile: 'fixture-fast'
		});
	}

	const conversations = {};
	const runDetails = {};
	const approvals = {};

	const attachment = { server_id: SERVER_A, op_id: OP_ID, sha256: ATTACHMENT_SHA, bytes: 1234 };

	if (scenario === 'replay') {
		// The canary rides as an UNKNOWN row field: the schema has no message
		// body, the panel never enumerates fields — the DOM must stay clean.
		conversations[ROOT] = Object.assign(
			runRow(ROOT, SERVER_A, 'running', {
				requested_grants: ['fixture.read', 'fixture.exec'],
				children: [childRow(CHILD_A, ROOT, 'succeeded'), childRow(CHILD_B, ROOT, 'failed')],
				attachments: [attachment]
			}),
			{ message: CANARY }
		);
	} else if (scenario === 'history') {
		conversations[HISTORY_RUN] = runRow(HISTORY_RUN, SERVER_A, 'running', {
			children: [childRow(CHILD_A, HISTORY_RUN, 'succeeded')],
			attachments: [attachment],
			// Recorded @service chips: the floating history selector restores
			// them as locked chips.
			service_tags: ['enshrouded', 'nginx']
		});
	} else if (scenario === 'approval' || scenario === 'approval-absent') {
		conversations[ROOT] = runRow(ROOT, SERVER_A, 'waiting_approval', {
			requested_grants: ['fixture.read', 'fixture.exec']
		});
		runDetails[ROOT] = {
			run_id: ROOT,
			server_id: SERVER_A,
			parent_run_id: null,
			depth: 0,
			profile_revision: 3,
			prompt_revision: 1,
			model_profile: 'fixture-fast',
			requested_grants: ['fixture.read', 'fixture.exec'],
			state: 'waiting_approval',
			cancel_requested_at: null,
			submitted_at: '2026-10-16T09:00:00Z',
			updated_at: '2026-10-16T09:02:00Z',
			steps: [
				{
					seq: 1,
					capability_id: 'fixture.exec',
					state: 'failed',
					receipt:
						scenario === 'approval'
							? {
									v: 1,
									op_id: OP_ID,
									capability_id: 'fixture.exec',
									payload_hash: PAYLOAD_SHA,
									policy_revision: 7,
									approval_id: APPROVAL_ID
								}
							: {
									v: 1,
									op_id: OP_ID,
									capability_id: 'fixture.exec',
									payload_hash: PAYLOAD_SHA,
									policy_revision: 7,
									approval_id: null
								}
				}
			]
		};
		if (scenario === 'approval') {
			approvals[APPROVAL_ID] = {
				approval_id: APPROVAL_ID,
				capability_id: 'fixture.exec',
				server_id: SERVER_A,
				payload_hash: PAYLOAD_SHA,
				policy_revision: 7,
				status: 'pending',
				created_by: 'fixture-admin',
				created_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
				// One hour ahead of the real clock: a hardcoded date goes stale
				// and the panel correctly refuses a decision on an expired
				// approval (the honest-expiry behavior this fixture must not trip).
				expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
				consumed_by: null,
				consumed_at: null
			};
		}
	}

	// --- in-page canonical conversation store (§3; task 10 fixture) ----------
	// Present only when the init script sets __maiChatCanonical: legacy
	// scenarios keep the exact legacy-only bag, so the panel's fallback
	// stays the behavior under test there (graceful degradation).

	const CANON_CONV = 'dd444444-1111-4222-8333-444455556666';
	const CANON_GAP = 'dd555555-1111-4222-8333-444455556666';
	const canonical = window.__maiChatCanonical === true;
	const canonicalStore = {};
	let canonicalRevision = 1;
	let canonicalBusy = false;
	const canonicalSteers = {};
	let canonicalPauseCount = 0;
	let canonicalCounter = 0;
	/** One-shot: the next canonical LIST resolves only after this delay. */
	let slowListDelay = 0;

	function msgId() {
		canonicalCounter += 1;
		return 'e1000000-0000-4000-8000-' + String(canonicalCounter).padStart(12, '0');
	}

	function canonRow(conversationId, serverId, title, completeness, gaps) {
		return {
			conversation_id: conversationId,
			server_id: serverId,
			title,
			created_at: '2026-10-16T08:00:00Z',
			updated_at: '2026-10-16T08:30:00Z',
			message_count: 0,
			active_task_id: null,
			active_run_id: null,
			history_completeness: completeness,
			missing_range: null,
			legacy_gaps: gaps,
			messages: [],
			events: [],
			latestSeq: 0
		};
	}

	function canonAppend(row, role, content, extras) {
		row.latestSeq += 1;
		const message = Object.assign(
			{
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
			},
			extras ?? {}
		);
		row.messages.push(message);
		row.message_count = row.messages.length;
		row.events.push({
			seq: row.latestSeq,
			kind: 'transcript.appended',
			message_id: message.message_id,
			role,
			source_kind: message.source_kind,
			redacted: message.redacted,
			has_content_ref: message.content_ref !== null,
			created_at: message.created_at
		});
		row.updated_at = message.created_at;
		return message;
	}

	function canonActiveRun(row) {
		return row.active_run_id !== null &&
			conversations[row.active_run_id] !== undefined &&
			!['succeeded', 'failed', 'cancelled'].includes(conversations[row.active_run_id].state)
			? row.active_run_id
			: null;
	}

	if (canonical) {
		const restored = canonRow(CANON_CONV, SERVER_A, 'Restore me', 'complete', []);
		for (let i = 1; i <= 7; i++) {
			canonAppend(restored, 'user', 'Restore question ' + i);
			canonAppend(restored, 'assistant', 'Restore answer ' + i + ' — recorded server-side.');
		}
		canonicalStore[CANON_CONV] = restored;
		const gappy = canonRow(CANON_GAP, SERVER_A, 'Partial legacy import', 'partial', [
			{
				kind: 'missing_range',
				from_sequence: 2,
				to_sequence: 5,
				count: null,
				description:
					'Sequences 2–5 predate the canonical store; browser-reconstructed bodies are unrecoverable.'
			}
		]);
		canonAppend(gappy, 'user', 'Legacy-era question');
		canonAppend(gappy, 'assistant', 'Legacy-era answer');
		canonicalStore[CANON_GAP] = gappy;
	}

	const canonicalApi = {
		async listCanonicalConversations(serverId, page) {
			calls.push({
				op: 'listCanonicalConversations',
				serverId,
				payload: JSON.parse(JSON.stringify(page ?? {}))
			});
			if (slowListDelay > 0) {
				const delay = slowListDelay;
				slowListDelay = 0;
				const resolvers = Promise.withResolvers();
				setTimeout(resolvers.resolve, delay);
				await resolvers.promise;
			}
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
			consumeReadFailure();
			const row = canonicalStore[String(conversationId)];
			// C8: the scope-blind 404 — never an existence or content leak.
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
				legacy_gaps: JSON.parse(JSON.stringify(row.legacy_gaps))
			};
		},
		async listConversationMessages(conversationId, serverId, page) {
			calls.push({
				op: 'listConversationMessages',
				runId: conversationId,
				serverId,
				payload: JSON.parse(JSON.stringify(page ?? {}))
			});
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
			calls.push({
				op: 'listConversationEvents',
				runId: conversationId,
				serverId,
				payload: JSON.parse(JSON.stringify(query ?? {}))
			});
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
					pending_steering: canonicalPauseCount
				}
			};
		},
		async createConversation(payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'createConversation', payload: wire });
			if (!UUID_RE.test(String(payload.client_request_id))) {
				throw new ApiError(422, 'validation_failed', 'client_request_id must be a canonical UUID string');
			}
			if (
				payload.server_id !== SERVER_A &&
				payload.server_id !== SERVER_B &&
				payload.server_id !== GLOBAL_CONSULT
			) {
				throw new ApiError(404, 'not_found', 'no canonical profile for the target server');
			}
			if (typeof payload.message !== 'string' || payload.message.length === 0) {
				throw new ApiError(422, 'validation_failed', 'message failed validation', '', {
					message: 'min_length'
				});
			}
			if (payload.expected_revision != null && payload.expected_revision !== canonicalRevision) {
				throw new ApiError(409, 'revision_conflict', 'expected task revision does not match the current revision', '', {
					expected_revision: 'revision_conflict'
				});
			}
			const id = String(payload.client_request_id);
			if (canonicalStore[id] !== undefined) {
				const first = canonicalStore[id].messages[0];
				if (first.content !== payload.message) {
					throw new ApiError(409, 'client_request_conflict', 'idempotency key is already used by a different submission');
				}
				return {
					conversation_id: id,
					message_id: first.message_id,
					task_id: null,
					run_id: id,
					revision: canonicalRevision,
					disposition: 'deduplicated'
				};
			}
			const row = canonRow(id, payload.server_id, null, 'complete', []);
			const message = canonAppend(row, 'user', payload.message);
			canonicalStore[id] = row;
			// The durable run: the facade row the run read and the SSE route serve.
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
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'submitConversationMessage', runId: conversationId, payload: wire });
			const row = canonicalStore[String(conversationId)];
			if (!row || row.server_id !== payload.server_id) {
				throw new ApiError(404, 'not_found', 'no conversation in the requesting server scope');
			}
			if (row.history_completeness !== 'complete') {
				// §4 gate: gap confirmation comes BEFORE any mutation (C7).
				throw new ApiError(
					409,
					'history_reconstruction_incomplete',
					'history reconstruction is ' +
						row.history_completeness +
						'; explicit user scope confirmation is required before new mutations',
					'',
					{ history_completeness: 'history_reconstruction_incomplete' }
				);
			}
			if (payload.expected_revision != null && payload.expected_revision !== canonicalRevision) {
				throw new ApiError(409, 'revision_conflict', 'expected task revision does not match the current revision', '', {
					expected_revision: 'revision_conflict'
				});
			}
			const prior = row.messages.find((one) => one.client_request_id === payload.client_request_id);
			if (prior !== undefined) {
				if (prior.content !== payload.message) {
					throw new ApiError(409, 'client_request_conflict', 'idempotency key is already used by a different submission');
				}
				return {
					conversation_id: row.conversation_id,
					message_id: prior.message_id,
					task_id: null,
					run_id: null,
					revision: canonicalRevision,
					disposition: 'deduplicated'
				};
			}
			const message = canonAppend(row, 'user', payload.message, {
				client_request_id: payload.client_request_id
			});
			const lease = canonActiveRun(row);
			if (canonicalBusy && lease !== null) {
				// Durable delivery identity: the lease-holding run consumes the
				// turn at its next safe boundary (202 semantics; no second executor).
				canonicalPauseCount += 1;
				return {
					conversation_id: row.conversation_id,
					message_id: message.message_id,
					task_id: null,
					run_id: null,
					revision: canonicalRevision,
					disposition: 'queued_followup'
				};
			}
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
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'steerConversation', runId: conversationId, payload: wire });
			const row = canonicalStore[String(conversationId)];
			if (!row || row.server_id !== payload.server_id) {
				throw new ApiError(404, 'not_found', 'no conversation in the requesting server scope');
			}
			const active = canonActiveRun(row);
			if (active === null) {
				throw new ApiError(409, 'no_active_run', "no active run holds this conversation's writer lease", '', {
					conversation_id: 'no_active_run'
				});
			}
			const key = String(payload.client_request_id);
			if (canonicalSteers[key] === true) {
				return {
					conversation_id: row.conversation_id,
					steering_id: key,
					run_id: active,
					action: payload.action,
					client_request_id: key,
					disposition: 'deduplicated',
					delivery_status: 'pending'
				};
			}
			canonicalSteers[key] = true;
			if (payload.action === 'cancel') {
				conversations[active].cancel_requested_at = '2026-10-16T09:05:00Z';
				conversations[active].state = 'cancelled';
				row.active_run_id = null;
			} else if (payload.action === 'pause') {
				canonicalPauseCount += 1;
			}
			return {
				conversation_id: row.conversation_id,
				steering_id: key,
				run_id: active,
				action: payload.action,
				client_request_id: key,
				disposition: 'created',
				delivery_status: 'pending'
			};
		}
	};

	// --- §8 task fixture slice (24b D-2) --------------------------------------
	// Rides the bag ONLY when the init script sets __maiChatTasks: exactly the
	// slice the composed desktop binds from the real client. ONE plan-only
	// episode per canonical conversation, scope-checked like the backend
	// (absent row, foreign scope → the SAME scope-blind 404 task_not_found),
	// bounded pages, and an HONEST evidence page (total 0 — no outcome
	// document exists for a plan-only episode and none is fabricated).
	const TASKS_BOUND = window.__maiChatTasks === true;
	const TASK_STEP_A = 'inventory-dependent-jobs';
	const TASK_STEP_B = 'disable-and-remove-unit';
	function taskIdOf(conversationId) {
		return 'f1000000-0000-4000-8000-' + String(conversationId).replace(/-/g, '').slice(0, 12);
	}
	function rowByTask(taskId) {
		const wanted = String(taskId);
		return (
			Object.values(canonicalStore).find(
				(row) => taskIdOf(row.conversation_id) === wanted
			) ?? null
		);
	}
	function taskOf(row) {
		return {
			task_id: taskIdOf(row.conversation_id),
			conversation_id: row.conversation_id,
			server_id: row.server_id,
			revision: 3,
			title: 'Fixture plan-only task',
			summary: 'Understood: repair the dock; constraints recorded; plan proposed.',
			requested_phase: 'act',
			status: 'in_progress',
			active_step_id: TASK_STEP_B,
			counts: {
				steps_total: 2,
				steps_executed: 1,
				constraints_total: 1,
				evidence_total: 0,
				criteria_total: 2,
				criteria_verified: 0
			},
			created_at: '2026-10-16T08:00:00Z',
			updated_at: '2026-10-16T08:30:00Z'
		};
	}
	function taskNotFound() {
		return new ApiError(
			404,
			'task_not_found',
			'no active task episode in the requesting server scope'
		);
	}
	const taskApi = {
		async getConversationTask(conversationId, serverId) {
			calls.push({ op: 'getConversationTask', runId: conversationId, serverId });
			const row = canonicalStore[String(conversationId)];
			if (!row || row.server_id !== serverId) throw taskNotFound();
			return clone(taskOf(row));
		},
		async listTaskSteps(taskId, serverId, query) {
			calls.push({
				op: 'listTaskSteps',
				runId: taskId,
				serverId,
				payload: JSON.parse(JSON.stringify(query ?? {}))
			});
			const row = rowByTask(taskId);
			if (!row || row.server_id !== serverId) throw taskNotFound();
			const items = [
				{
					task_id: taskId,
					step_id: TASK_STEP_A,
					title: 'Inventory dependent jobs',
					status: 'executed',
					verification: 'passed',
					position: 1,
					risk: 'read_only',
					prerequisite_unresolved: false,
					expected_check: 'cron inventory shows no dependent job',
					skip_reason: null,
					result_summary: 'No downstream job references the legacy unit.',
					evidence_refs: [
						{
							kind: 'observation',
							evidence_id: '9f8e7d6c-5a4b-4c3d-8e2f-1a2b3c4d5e6f',
							note: null
						}
					],
					depends_on: [],
					superseded_at: null,
					superseded_by: null,
					created_at: '2026-10-16T08:00:00Z',
					updated_at: '2026-10-16T08:10:00Z'
				},
				{
					task_id: taskId,
					step_id: TASK_STEP_B,
					title: 'Disable and remove the unit',
					status: 'in_progress',
					verification: 'not_checked',
					position: 2,
					risk: 'mutating',
					prerequisite_unresolved: false,
					expected_check: null,
					skip_reason: null,
					result_summary: null,
					evidence_refs: [],
					depends_on: [TASK_STEP_A],
					superseded_at: null,
					superseded_by: null,
					created_at: '2026-10-16T08:00:00Z',
					updated_at: '2026-10-16T08:20:00Z'
				}
			];
			const limit = query?.limit ?? 10;
			const offset = query?.offset ?? 0;
			const sliced = items.slice(offset, offset + limit);
			return {
				task_id: taskId,
				limit,
				offset,
				total: items.length,
				has_more: offset + sliced.length < items.length,
				items: clone(sliced)
			};
		},
		async listTaskConstraints(taskId, serverId, query) {
			calls.push({
				op: 'listTaskConstraints',
				runId: taskId,
				serverId,
				payload: JSON.parse(JSON.stringify(query ?? {}))
			});
			const row = rowByTask(taskId);
			if (!row || row.server_id !== serverId) throw taskNotFound();
			const items = [
				{
					task_id: taskId,
					constraint_id: 'no-scheduler-restart',
					category: 'safety',
					statement: 'Never restart the production scheduler while the unit is disabled.',
					origin: 'user',
					source_message_refs: [row.messages.find((one) => one.role === 'user')?.message_id ?? ''],
					position: 1,
					superseded_at: null,
					superseded_by: null,
					created_at: '2026-10-16T08:00:00Z',
					updated_at: '2026-10-16T08:00:00Z'
				}
			];
			const limit = query?.limit ?? 10;
			const offset = query?.offset ?? 0;
			const sliced = items.slice(offset, offset + limit);
			return {
				task_id: taskId,
				limit,
				offset,
				total: items.length,
				has_more: offset + sliced.length < items.length,
				items: clone(sliced)
			};
		},
		async listTaskEvidence(taskId, serverId, page) {
			calls.push({
				op: 'listTaskEvidence',
				runId: taskId,
				serverId,
				payload: JSON.parse(JSON.stringify(page ?? {}))
			});
			const row = rowByTask(taskId);
			if (!row || row.server_id !== serverId) throw taskNotFound();
			return {
				task_id: taskId,
				limit: page?.limit ?? 10,
				offset: page?.offset ?? 0,
				// The plan-only honesty: zero recorded evidence rows.
				total: 0,
				has_more: false,
				items: []
			};
		},
		async listTaskRevisions(taskId, serverId, page) {
			calls.push({
				op: 'listTaskRevisions',
				runId: taskId,
				serverId,
				payload: JSON.parse(JSON.stringify(page ?? {}))
			});
			const row = rowByTask(taskId);
			if (!row || row.server_id !== serverId) throw taskNotFound();
			const items = [
				{
					task_id: taskId,
					revision: 1,
					kind: 'create',
					author: 'model',
					source_message_id: null,
					item_id: null,
					payload: null,
					applied_at: '2026-10-16T08:00:00Z'
				}
			];
			const limit = page?.limit ?? 10;
			const offset = page?.offset ?? 0;
			const sliced = items.slice(offset, offset + limit);
			return {
				task_id: taskId,
				limit,
				offset,
				total: items.length,
				has_more: offset + sliced.length < items.length,
				items: clone(sliced)
			};
		},
		async submitTaskCorrection(taskId, payload) {
			calls.push({
				op: 'submitTaskCorrection',
				runId: taskId,
				payload: JSON.parse(JSON.stringify(payload))
			});
			const row = rowByTask(taskId);
			if (!row || row.server_id !== payload.server_id) throw taskNotFound();
			const header = taskOf(row);
			return {
				task: Object.assign(clone(header), { revision: header.revision + 1 }),
				revision: {
					task_id: taskId,
					revision: header.revision + 1,
					kind: 'plan_patch',
					author: 'user',
					source_message_id: payload.patch?.source_message_id ?? null,
					item_id: null,
					payload: null,
					applied_at: '2026-10-16T09:30:00Z'
				},
				disposition: 'created'
			};
		},
		async submitTaskDecision(taskId, payload) {
			calls.push({
				op: 'submitTaskDecision',
				runId: taskId,
				payload: JSON.parse(JSON.stringify(payload))
			});
			const row = rowByTask(taskId);
			if (!row || row.server_id !== payload.server_id) throw taskNotFound();
			const header = taskOf(row);
			return {
				task: clone(header),
				revision: {
					task_id: taskId,
					revision: header.revision,
					kind: 'metadata',
					author: 'user',
					source_message_id: null,
					item_id: null,
					payload: null,
					applied_at: '2026-10-16T09:30:00Z'
				},
				disposition: 'created'
			};
		}
	};

	let approvalReadFailure = null;
	let failNextRead = null;
	let decideFailure = null;
	let cancelOutcome = null;

	const api = {
		// The canonical slice rides ONLY when the scenario mounted the
		// canonical store: the legacy bag stays byte-identical otherwise.
		...(canonical ? canonicalApi : {}),
		// The §8 task slice rides ONLY when the init script binds it (24b
		// D-2): the unbound path stays the fail-closed behavior under test.
		...(TASKS_BOUND ? taskApi : {}),
		async submitMessage(payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'submitMessage', payload: wire });
			consumeReadFailure();
			// Producer strictness mirrors (mai.api.chat submit_message).
			if (!UUID_RE.test(String(payload.run_id))) {
				throw new ApiError(422, 'validation_failed', 'run_id must be a canonical UUID string');
			}
			if (
				payload.server_id !== SERVER_A &&
				payload.server_id !== SERVER_B &&
				payload.server_id !== GLOBAL_CONSULT
			) {
				throw new ApiError(404, 'not_found', 'no canonical profile for the target server');
			}
			if (typeof payload.message !== 'string' || payload.message.length === 0) {
				throw new ApiError(422, 'validation_failed', 'message failed validation', '', { message: 'min_length' });
			}
			if (Array.isArray(payload.children) && payload.children.length > 8) {
				throw new ApiError(422, 'validation_failed', 'children failed validation', '', { children: 'max_items' });
			}
			const runId = String(payload.run_id);
			const serverId = String(payload.server_id);
			const childIds = (payload.children ?? []).map(() => childId());
			const rows = childIds.map((id) => childRow(id, runId, 'queued', serverId));
			const attachments = (payload.attachments ?? []).map((one) => ({
				server_id: serverId,
				op_id: String(one.op_id),
				sha256: ATTACHMENT_SHA,
				bytes: 1234
			}));
			const conversation = runRow(runId, serverId, 'running', {
				requested_grants: payload.allowed_capabilities ?? [],
				children: rows,
				attachments,
				// The producer records the submitted @service chips on the run
				// row; a restore reads them back (absent ⇒ older row, no tags).
				...(Array.isArray(payload.service_tags) ? { service_tags: [...payload.service_tags] } : {})
			});
			conversations[runId] = conversation;
			submissions.push({ payload: wire, childIds });
			return { conversation: clone(conversation), deduplicated: false };
		},
		async listConversations(serverId, page) {
			calls.push({ op: 'listConversations', serverId, payload: page ?? {} });
			consumeReadFailure();
			const rows = Object.values(conversations)
				.filter((row) => row.server_id === serverId)
				.sort((a, b) => a.submitted_at.localeCompare(b.submitted_at));
			const limit = page?.limit ?? 10;
			const offset = page?.offset ?? 0;
			const sliced = rows.slice(offset, offset + limit);
			return {
				conversations: clone(sliced),
				limit,
				offset,
				total: rows.length
			};
		},
		async readConversation(runId) {
			calls.push({ op: 'readConversation', runId });
			consumeReadFailure();
			const row = conversations[runId];
			if (!row) throw new ApiError(404, 'not_found', 'no run stored for this run id');
			return clone(row);
		},
		async cancelConversation(runId, payload) {
			calls.push({ op: 'cancelConversation', runId, payload: JSON.parse(JSON.stringify(payload)) });
			if (!conversations[runId]) throw new ApiError(404, 'not_found', 'no run stored for this run id');
			// Producer strictness: the explicit tree-cancel body is REQUIRED.
			if (payload.cancel_children !== true) {
				throw new ApiError(422, 'validation_failed', 'cancel_children must be true');
			}
			if (conversations[runId].cancel_requested_at === null) {
				conversations[runId].cancel_requested_at = '2026-10-16T09:05:00Z';
			}
			const outcome = cancelOutcome;
			cancelOutcome = null;
			return {
				run_id: runId,
				state: conversations[runId].state,
				outcome,
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
			consumeReadFailure();
			const detail = runDetails[runId];
			if (detail) return clone(detail);
			const row = conversations[runId];
			if (!row) throw new ApiError(404, 'not_found', 'no run stored for this run id');
			return Object.assign(clone(row), { steps: [] });
		},
		async readApproval(approvalId) {
			calls.push({ op: 'readApproval', runId: approvalId });
			if (approvalReadFailure) {
				const { status, code, message } = approvalReadFailure;
				throw new ApiError(status, code, message);
			}
			consumeReadFailure();
			const row = approvals[approvalId];
			if (!row) throw new ApiError(404, 'not_found', 'no approval record for this id');
			return clone(row);
		},
		async decideApproval(approvalId, payload) {
			const wire = JSON.parse(JSON.stringify(payload));
			calls.push({ op: 'decideApproval', runId: approvalId, payload: wire });
			if (decideFailure) {
				const failure = decideFailure;
				decideFailure = null;
				throw new ApiError(failure.status, failure.code, failure.message);
			}
			const row = approvals[approvalId];
			if (!row) throw new ApiError(404, 'not_found', 'no approval record for this id');
			if (row.status !== 'pending') {
				throw new ApiError(409, 'already_consumed', 'decision already consumed');
			}
			// The binding is confirmed field-for-field (C03 rule 4).
			if (
				payload.capability_id !== row.capability_id ||
				payload.server_id !== row.server_id ||
				payload.payload_hash !== row.payload_hash ||
				payload.policy_revision !== row.policy_revision
			) {
				throw new ApiError(409, 'payload_mismatch', 'presented binding mismatches the approval');
			}
			row.status = 'consumed';
			row.consumed_by = 'fixture-admin';
			row.consumed_at = '2026-10-16T09:06:00Z';
			return clone(row);
		}
	};

	// The bag carries the optional draft-context extras (server options for
	// the picker, mapped services for the @service autocomplete) alongside
	// the required api slice — the composed desktop passes the same shape.
	// 'live' mirrors DesktopShell: only a live session's slice marks the
	// panel free to fetch the configured model profiles.
	const services = {
		...chatServices(api),
		live: window.__maiChatLive === true,
		serverOptions: [
			{ id: SERVER_A, label: 'Server Alpha' },
			{ id: SERVER_B, label: 'Server Beta' }
		],
		availableServices: [
			{ name: 'enshrouded', category: 'games' },
			{ name: 'nginx', category: 'web' },
			{ name: 'postgres', category: 'databases' }
		]
	};

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
		chatEntry(),
		{
			panel_kind: 'fixture-notes',
			title: 'Notes',
			scope_types: ['server'],
			component: () => Promise.resolve(sibling)
		}
	]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	const registry = registryResult.value;

	let showMain = $state(true);
	let showSecond = $state(true);

	const WINDOWS = {
		main: '6a6b7c8d-9e0f-4a1b-8c3d-4e5f6a7b8c9d',
		second: '7b7c8d9e-0f1a-4b2c-9d4e-5f6a7b8c9d0e',
		bare: '8c8d9e0f-1a2b-4c3d-8e5f-6a7b8c9d0e1f',
		global: '9d9e0f1a-2b3c-4d4e-9f6a-7b8c9d0e1f2a',
		sibling: 'a0af1b2c-3d4e-4e5f-8a7b-8c9d0e1f2a3b'
	};

	function descriptor(windowId, scope, z) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: 'chat',
			scope,
			geometry: { x: 8, y: 8, width: 780, height: 620 },
			presentation: 'normal',
			z,
			title_override: null
		};
	}

	const descriptorSibling = {
		schema_version: 1,
		window_id: WINDOWS.sibling,
		panel_kind: 'fixture-notes',
		scope: { kind: 'server', server_id: '${SERVER_A}' },
		geometry: { x: 16, y: 16, width: 320, height: 240 },
		presentation: 'normal',
		z: 5,
		title_override: null
	};

	window.__maiChatApi = {
		calls,
		streams,
		submissions,
		setConversationState(runId, state) {
			if (!conversations[runId]) throw new Error('no such fixture conversation');
			conversations[runId].state = state;
			conversations[runId].updated_at = '2026-10-16T09:07:00Z';
		},
		setApprovalFields(fields) {
			Object.assign(approvals[APPROVAL_ID], fields);
		},
		failApprovalRead(failure) {
			approvalReadFailure = failure;
		},
		armCancel(outcome) {
			cancelOutcome = outcome;
		},
		failNextRead(failure) {
			failNextRead = failure;
		},
		armDecideFailure(failure) {
			decideFailure = failure;
		},
		toggleMain(on) {
			showMain = on;
		},
		toggleSecond(on) {
			showSecond = on;
		},
		seedConversations(serverId, count) {
			for (let i = 1; i <= count; i++) {
				const id = '00000000-0000-4000-8000-' + String(i).padStart(12, '0');
				conversations[id] = runRow(id, serverId, 'succeeded', {
					submitted_at: '2026-10-16T09:' + String(i).padStart(2, '0') + ':00Z',
					service_tags: ['svc-' + i],
					children: [],
					attachments: []
				});
			}
		},
		armRevision(next) {
			canonicalRevision = next;
		},
		armBusy(on) {
			canonicalBusy = on;
		},
		appendAssistant(conversationId, content) {
			const row = canonicalStore[String(conversationId)];
			if (!row) throw new Error('no such canonical conversation');
			canonAppend(row, 'assistant', content);
		},
		completeRun(runId) {
			const run = conversations[String(runId)];
			if (run === undefined) throw new Error('no such fixture run');
			run.state = 'succeeded';
			const row = canonicalStore[String(runId)];
			if (row !== undefined && row.active_run_id === String(runId)) row.active_run_id = null;
		},
		lastCanonicalId() {
			const ids = Object.keys(canonicalStore).filter(
				(id) => id !== CANON_CONV && id !== CANON_GAP
			);
			return ids.length === 0 ? null : ids[ids.length - 1];
		},
		async probeCrossServer() {
			// Drives the REAL harness getConversation across scopes; returns
			// ONLY the structured code — never conversation content (C8).
			try {
				await canonicalApi.getConversation(CANON_CONV, SERVER_B);
				return { code: 'leaked' };
			} catch (error) {
				return {
					code:
						error !== null && typeof error === 'object' && 'code' in error
							? String(error.code)
							: 'unknown'
				};
			}
		},
		async probeCrossServerTask() {
			// Drives the REAL harness task header read across scopes; returns
			// ONLY the structured code — the scope-blind 404 is the contract
			// (24b D-2, C8).
			try {
				await taskApi.getConversationTask(CANON_CONV, SERVER_B);
				return { code: 'leaked' };
			} catch (error) {
				return {
					code:
						error !== null && typeof error === 'object' && 'code' in error
							? String(error.code)
							: 'unknown'
				};
			}
		},
		seedCanonicalConversations(count) {
			for (let i = 1; i <= count; i++) {
				const id = 'cc000000-0000-4000-8000-' + String(i).padStart(12, '0');
				const row = canonRow(
					id,
					SERVER_A,
					'Paged conversation ' + i,
					'complete',
					[]
				);
				row.updated_at = '2026-10-16T07:' + String(59 - i).padStart(2, '0') + ':00Z';
				canonicalStore[id] = row;
			}
		},
		seedCanonicalRow(serverId, conversationId, title) {
			const row = canonRow(String(conversationId), String(serverId), String(title), 'complete', []);
			canonAppend(row, 'user', 'Seeded question — ' + title);
			canonAppend(row, 'assistant', 'Seeded recorded answer — ' + title);
			canonicalStore[String(conversationId)] = row;
		},
		armSlowList(delayMs) {
			slowListDelay = Number(delayMs);
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
</script>

<section data-testid="host-main" aria-label="Host main">
	<ComponentHost label="Host main">
		{#if showMain}
			{#snippet mainPanel()}
				<PanelHost {registry} descriptor={descriptor(WINDOWS.main, { kind: 'server', server_id: '${SERVER_A}' }, 1)} services={services} />
			{/snippet}
			{#if window.__maiChatFramed}
				<WindowFrame
					descriptor={{...descriptor(WINDOWS.main, {kind:'server', server_id:'${SERVER_A}'}, 1), geometry: {x:0,y:0,width: Math.min(1100, window.innerWidth),height:window.innerHeight}}}
					title="Conversation" contextLabel="Server Alpha" autoFitHeight
				>{@render mainPanel()}</WindowFrame>
			{:else}
				{@render mainPanel()}
			{/if}
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-second" aria-label="Host second">
	<ComponentHost label="Host second">
		{#if showSecond}
			<PanelHost
				{registry}
				descriptor={descriptor(WINDOWS.second, { kind: 'server', server_id: '${SERVER_B}' }, 2)}
				services={services}
			/>
		{/if}
	</ComponentHost>
</section>
<section data-testid="host-bare" aria-label="Host bare">
	<ComponentHost label="Host bare">
		<PanelHost
			{registry}
			descriptor={descriptor(WINDOWS.bare, { kind: 'server', server_id: '${SERVER_A}' }, 3)}
		/>
	</ComponentHost>
</section>
<section data-testid="host-global" aria-label="Host global">
	<ComponentHost label="Host global">
		<PanelHost
			{registry}
			descriptor={descriptor(WINDOWS.global, { kind: 'global' }, 4)}
			services={services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-sibling" aria-label="Host sibling">
	<ComponentHost label="Host sibling">
		<PanelHost {registry} descriptor={descriptorSibling} />
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
	data?: Record<string, unknown>;
}

interface StreamSession {
	/** Frames to deliver before the stream closes; empty for a bare close. */
	frames?: StreamFrame[];
	/** Reject the connection (API restart / network failure) instead of closing. */
	abort?: boolean;
	/**
	 * Deliver EVERY queued frame regardless of the recorded cursor. The
	 * reviewed producer is cursor-exclusive; a connection that re-delivers
	 * anyway (proxy replay, buggy cursor) must still be deduplicated by the
	 * consumer watermark — the reconnect test drives exactly that.
	 */
	replayAll?: boolean;
}

let streamSessions: StreamSession[] = [];
let streamRequests: Array<{ runId: string; cursor: string | null; path: string }> = [];
/** Requests held open because no session was queued yet (the loop pauses). */
let heldStreamRoutes: Array<{ route: Route; runId: string; cursor: string | null }> = [];

function frame(seq: number, type: string, data: Record<string, unknown>): StreamFrame {
	return { seq, type, data };
}

function sseBody(runId: string, frames: StreamFrame[], cursor: string | null): string {
	// Producer replay semantics: only frames strictly after the cursor travel
	// on a resumed subscription (C08 rule 7). The consumer watermark is the
	// second line of defense and is proven separately.
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
		body: sseBody(runId, session.frames ?? [], session.replayAll ? null : cursor)
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
		else await fulfillStream(route, session, match[1], cursor);
		return;
	}
	heldStreamRoutes.push({ route, runId: match[1], cursor });
}

/**
 * Queue TWO copies of the session: both live subscribers of the root stream
 * (the panel fold and the composed tree fold) each consume one. Each keeps
 * its own watermark, so identical frames are applied exactly once per view.
 */
function queueStreamPair(session: StreamSession): void {
	queueStreamSession(session);
	queueStreamSession(session);
}

/** Queue a session and release the oldest held request with it. */
function queueStreamSession(session: StreamSession): void {
	streamSessions.push(session);
	const held = heldStreamRoutes.shift();
	if (!held) return;
	const next = streamSessions.shift();
	if (!next) {
		heldStreamRoutes.unshift(held);
		return;
	}
	void (next.abort
		? held.route.abort()
		: fulfillStream(held.route, next, held.runId, held.cursor)
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
		name: 'mai-chat-harness',
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
			outDir: 'node_modules/.mai-chat-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'chat-[hash].js',
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
	streamSessions = [];
	streamRequests = [];
	heldStreamRoutes = [];

	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAI chat fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"></script>
</body>
</html>`;

	// Serve the virtual app; every non-fixture request aborts so the panel can
	// never silently depend on some other origin.
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
	// Surface runtime failures in the receipt: an uncaught page error is a
	// harness/component defect, never a silent pass.
	page.on('pageerror', (error) => {
		throw new Error(`fixture page error: ${error.message}`);
	});
});

async function openFixture(
	page: Page,
	scenario: Scenario,
	opts?: { tasks?: boolean }
): Promise<Page> {
	await page.clock.setFixedTime(new Date('2026-10-16T09:30:00Z'));
	await page.addInitScript(
		(value) => {
			window.__maiChatScenario = value.scenario;
			window.__maiChatCanonical = value.scenario === 'canonical';
			window.__maiChatTasks = value.tasks === true;
		},
		{ scenario, tasks: opts?.tasks === true }
	);
	await page.goto(FIXTURE_URL);
	await expect(page.getByTestId('host-main').getByTestId('chat-root')).toBeVisible();
	await expect(page.getByTestId('host-second').getByTestId('chat-root')).toBeVisible();
	return page;
}

/** Fill the MAIN compose form and submit; waits for the recorded call. */
async function submitFromHost(page: Page, host: string, message: string): Promise<void> {
	const scope = page.getByTestId(host);
	await scope.getByTestId('compose-message').fill(message);
	await scope.getByTestId('compose-profile').fill('fixture-fast');
	await scope.getByTestId('chat-submit').click();
	await expect
		.poll(async () => (await callsOf(page, 'submitMessage')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(1);
}

test('submit pins the window scope; the recorded conversation renders transcript facts and the composed tree', async ({
	page
}) => {
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');
	const second = page.getByTestId('host-second');

	// Boot: the two usable hosts read their OWN pinned scopes; nothing else.
	await expect
		.poll(async () => (await callsOf(page, 'listConversations')).map((call) => call.serverId).sort(), {
			timeout: 15000
		})
		.toEqual([SERVER_A, SERVER_B]);
	expect(await callsOf(page, 'submitMessage')).toHaveLength(0);
	await main.getByTestId('chat-history-toggle').click();
	await expect(main.getByTestId('history-empty')).toBeVisible();
	await main.getByTestId('chat-history-toggle').click();

	// Compose in MAIN: message, explicit profile, read-only OFF (default),
	// one attachment reference. Capabilities and children are NOT sent:
	// the backend auto-resolves the granted capability set, and the manual
	// spawn form is gone.
	await main.getByTestId('compose-message').fill('Review the fixture deployment end to end.');
	await main.getByTestId('compose-profile').fill('fixture-fast');
	await main.getByTestId('compose-read-only-toggle').click();
	await expect(main.getByTestId('compose-read-only-toggle')).toHaveAttribute('aria-pressed', 'true');
	await main.getByTestId('compose-read-only-toggle').click();
	await main.getByTestId('compose-attachments').fill(OP_ID);
	await main.getByTestId('chat-submit').click();

	await expect
		.poll(async () => (await callsOf(page, 'submitMessage')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(1);
	const submissions = await callsOf(page, 'submitMessage');
	const body = submissions[0].payload as Record<string, unknown>;
	expect(body.server_id).toBe(SERVER_A);
	expect(UUID_RE.test(String(body.run_id))).toBe(true);
	expect(body.message).toBe('Review the fixture deployment end to end.');
	expect(body.profile).toBe('fixture-fast');
	expect(body.role).toBe('administrator');
	expect(body.mode).toBe('delegated');
	expect(body.read_only).toBe(false);
	expect(body.allowed_capabilities).toBeUndefined();
	expect(body.children).toBeUndefined();
	expect(body.attachments).toEqual([{ server_id: SERVER_A, op_id: OP_ID }]);

	// The recorded conversation renders: pinned target, opener message,
	// recorded attachment handles (identity + integrity only).
	await expect(main.getByTestId('conversation-state')).toHaveText('Running');
	await expect(main.getByTestId('conversation-target')).toBeHidden();
	await main.getByTestId('chat-details-trigger').click();
	await expect(main.getByTestId('conversation-target')).toContainText(SERVER_A.slice(0, 8));
	await expect(main.getByTestId('conversation-message')).toHaveText(
		'Review the fixture deployment end to end.'
	);
	const attachmentRow = main.getByTestId('attachment-row');
	await expect(attachmentRow).toHaveCount(1);
	await expect(attachmentRow.getByTestId('attachment-op')).toHaveText(OP_ID.slice(0, 8));
	await expect(attachmentRow.getByTestId('attachment-sha')).toHaveText(
		`sha256 ${ATTACHMENT_SHA.slice(0, 12)}`
	);
	await expect(attachmentRow.getByTestId('attachment-bytes')).toHaveText('1234 bytes');

	// Stream the recorded facts (both live subscribers consume the pair).
	const submission = await page.evaluate(() => {
		const list = window.__maiChatApi!.submissions;
		return list[list.length - 1];
	});
	expect(submission.childIds).toEqual([]);
	await queueStreamPair({
		frames: [
			frame(1, 'chat.submitted', {
				submission_sha256: PAYLOAD_SHA,
				handler: 'agent.run',
				children: 0,
				attachments: [{ server_id: SERVER_A, op_id: OP_ID, sha256: ATTACHMENT_SHA, bytes: 1234 }]
			}),
			frame(2, 'agent.started', {
				connection: 'fixture-gateway',
				profile: 'fixture-fast',
				model: 'fixture-model-mini',
				role: 'administrator',
				rounds_budget: 8
			}),
			frame(3, 'agent.context_assembled', {
				prompt_revision: 1,
				prompt_sha256: PAYLOAD_SHA,
				profile_revision: 3,
				profile_sha256: PAYLOAD_SHA,
				connection_revision: 2,
				profile_model_revision: 1,
				truncated: false,
				budget: { context_limit_tokens: 8192, used_chars: 900 },
				evidence_items: 1
			}),
			frame(4, 'agent.model_round', {
				round: 1,
				status: 'tool_calls',
				tool_calls: [{ seq: 1, capability_id: 'fixture.exec', payload_hash: PAYLOAD_SHA }]
			})
		]
	});
	const transcript = main.getByTestId('transcript-list');
	// The transcript is a collapsed details accordion — expand it before
	// reading the recorded events.
	await main.getByTestId('conversation-transcript').locator('summary').click();
	// No child tasks were submitted: the planned event never arrives and the
	// transcript carries exactly the recorded run facts.
	await expect(transcript.getByTestId('transcript-entry')).toHaveCount(4);
	await expect(transcript.getByTestId('transcript-label')).toContainText([
		'Model bound',
		'Round 1 — tool calls requested'
	]);
	await expect(transcript.getByTestId('transcript-value')).toContainText([
		'fixture-model-mini',
		PAYLOAD_SHA.slice(0, 12)
	]);
	// The requested capability renders as the row KEY of the round entry.
	await expect(transcript.getByText('fixture.exec')).toBeVisible();

	// The composed (not re-implemented) tree shows the honest empty state:
	// no child runs exist for this childless submission.
	const tree = main.getByTestId('conversation-children');
	await expect(tree).toBeVisible();
	await expect(tree.getByTestId('children-none')).toContainText('No child runs recorded');

	// The SECOND window pins ITS OWN scope: its submit carries SERVER_B and a
	// different idempotency key — no hidden global current-server target.
	await second.getByTestId('compose-message').fill('Second window task');
	await second.getByTestId('compose-profile').fill('fixture-fast');
	await second.getByTestId('chat-submit').click();
	await expect
		.poll(async () => (await callsOf(page, 'submitMessage')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(2);
	const allSubmissions = await callsOf(page, 'submitMessage');
	const secondBody = allSubmissions[1].payload as Record<string, unknown>;
	expect(secondBody.server_id).toBe(SERVER_B);
	expect(secondBody.run_id).not.toBe(body.run_id);
});

test('hostile stream content renders inert; the context body never reaches the DOM', async ({
	page
}) => {
	await openFixture(page, 'replay');
	const main = page.getByTestId('host-main');

	// Open the recorded conversation from history (no authored labels here).
	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	await main.getByTestId('chat-details-trigger').click();
	await expect(main.getByTestId('conversation-id')).toBeVisible();

	// Round 1: benign value-free facts.
	await queueStreamPair({
		frames: [
			frame(1, 'chat.submitted', { submission_sha256: PAYLOAD_SHA, handler: 'agent.run', children: 0, attachments: [] }),
			frame(2, 'agent.started', {
				connection: 'fixture-gateway',
				profile: 'fixture-fast',
				model: 'fixture-model-mini',
				role: 'administrator',
				rounds_budget: 8
			})
		]
	});
	const transcript = main.getByTestId('transcript-list');
	await expect(transcript.getByTestId('transcript-entry')).toHaveCount(2);

	// Wait for the reconnects to be parked (the run still reads `running`,
	// so both loops stay alive), THEN record the terminal state and deliver
	// the hostile round: the durable refresh flips the badge afterwards.
	await waitForStreamRequests(4);
	await page.evaluate(() => window.__maiChatApi!.setConversationState('aa111111-1111-4222-8333-444455556666', 'failed'));
	await queueStreamPair({
		frames: [
			frame(3, 'step.completed', {
				capability_id: 'fixture.exec',
				reason: HOSTILE_TOOL_TEXT,
				op_id: OP_ID
			}),
			frame(4, 'agent.failed', { reason: HOSTILE_FAILURE })
		]
	});
	await expect(transcript.getByTestId('transcript-value')).toContainText([
		'MAI-FIXTURE-HOSTILE-TOOL-TEXT',
		'fixture_denial'
	]);

	// The hostile content is TEXT, never markup: no injected elements, no
	// executed handlers (sanitization negative control, brief acceptance 2).
	await expect(transcript.locator('img')).toHaveCount(0);
	await expect(transcript.locator('script')).toHaveCount(0);
	expect(await page.evaluate(() => window.__maiPwned)).toBeUndefined();

	// The wire-invisible context body never appears anywhere in the DOM.
	const bodyText = await page.evaluate(() => document.body.textContent ?? '');
	expect(bodyText.includes('MAI-FIXTURE-CANARY-context-body-NEVER-DISPLAY')).toBe(false);
});

test('reconnect resumes from Last-Event-ID; the watermark deduplicates replayed frames', async ({
	page
}) => {
	await openFixture(page, 'replay');
	const main = page.getByTestId('host-main');
	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	await main.getByTestId('chat-details-trigger').click();

	// Round 1 (fresh subscriptions, cursor absent).
	await queueStreamPair({
		frames: [
			frame(1, 'chat.submitted', { submission_sha256: PAYLOAD_SHA, handler: 'agent.run', children: 0, attachments: [] }),
			frame(2, 'agent.started', {
				connection: 'fixture-gateway',
				profile: 'fixture-fast',
				model: 'fixture-model-mini',
				role: 'administrator',
				rounds_budget: 8
			}),
			frame(3, 'agent.context_assembled', { prompt_revision: 1, truncated: false, evidence_items: 0 }),
			frame(4, 'agent.model_round', {
				round: 1,
				status: 'tool_calls',
				tool_calls: [{ seq: 1, capability_id: 'fixture.read', payload_hash: PAYLOAD_SHA }]
			})
		]
	});
	const transcript = main.getByTestId('transcript-list');
	await expect(transcript.getByTestId('transcript-entry')).toHaveCount(4);

	// Both subscribers saw the bounded close and resumed with the cursor:
	// the bag records the replay cursor per call, the route handler records
	// the actual Last-Event-ID header that traveled on the wire.
	await waitForStreamRequests(4);
	await expect
		.poll(
			async () =>
				(await callsOf(page, 'streamConversationEvents')).filter((call) => call.cursor === '4')
					.length,
			{ timeout: 15000 }
		)
		.toBeGreaterThanOrEqual(1);
	const headerCarry = streamRequests.filter((one) => one.cursor === '4');
	expect(headerCarry.length).toBeGreaterThanOrEqual(1);

	// Round 2: a connection that re-delivers the ALREADY-SEEN seq 4 next to
	// the new seq 5. The cursor-exclusivity the producer froze is what keeps
	// honest replays bounded; the consumer watermark is the second line of
	// defense — the duplicate must be dropped, the new frame applied once.
	await queueStreamPair({
		replayAll: true,
		frames: [
			frame(4, 'agent.model_round', {
				round: 1,
				status: 'tool_calls',
				tool_calls: [{ seq: 1, capability_id: 'fixture.read', payload_hash: PAYLOAD_SHA }]
			}),
			frame(5, 'agent.model_round', { round: 1, status: 'final', output_chars: 42 })
		]
	});
	await expect(transcript.getByTestId('transcript-entry')).toHaveCount(5);
	await expect(transcript.getByTestId('transcript-label')).toContainText([
		'Round 1 — final answer recorded'
	]);
	const seqs = await transcript.getByTestId('transcript-entry').evaluateAll((entries) =>
		entries.map((entry) => entry.getAttribute('data-seq'))
	);
	expect(new Set(seqs).size).toBe(seqs.length);
	expect(seqs).toContain('5');
});

test('approval path: the receipt binding renders field-for-field and the one-use decision consumes', async ({
	page
}) => {
	await openFixture(page, 'approval');
	const main = page.getByTestId('host-main');

	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	await expect(main.getByTestId('conversation-state')).toHaveText('Waiting for approval decision');

	await expect(main.getByTestId('approval-decide')).toBeVisible();
	await expect(main.getByTestId('approval-decide')).toHaveCount(1);
	await expect(main.getByTestId('chat-details-panel')).toBeHidden();
	await main.getByTestId('chat-details-trigger').click();
	const section = main.getByTestId('approval-section');
	await expect(section).toBeVisible();
	await expect(section.getByTestId('approval-absent')).toHaveCount(0);
	await expect(section.getByTestId('approval-id')).toHaveText(APPROVAL_ID);
	await expect(section.getByTestId('approval-capability')).toHaveText('fixture.exec');
	await expect(section.getByTestId('approval-server')).toHaveText(SERVER_A);
	await expect(section.getByTestId('approval-hash')).toHaveText(PAYLOAD_SHA);
	await expect(section.getByTestId('approval-revision')).toHaveText('7');

	await main.getByTestId('approval-decide').click();
	await expect(section.getByTestId('approval-spent')).toContainText('Decision consumed');
	await expect(main.getByTestId('approval-decide')).toHaveCount(0);
	await expect(main.getByTestId('approval-warning')).toHaveCount(0);
	const decisions = await callsOf(page, 'decideApproval');
	expect(decisions).toHaveLength(1);
	expect(decisions[0].payload).toEqual({
		capability_id: 'fixture.exec',
		server_id: SERVER_A,
		payload_hash: PAYLOAD_SHA,
		policy_revision: 7
	});
});

test('a refused one-use decision renders honestly and never invents a consume', async ({ page }) => {
	await openFixture(page, 'approval');
	const main = page.getByTestId('host-main');
	await page.evaluate(() =>
		window.__maiChatApi!.armDecideFailure({
			status: 409,
			code: 'already_consumed',
			message: 'decision already consumed'
		})
	);

	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	const section = main.getByTestId('approval-section');
	await expect(main.getByTestId('approval-decide')).toBeVisible();

	await main.getByTestId('approval-decide').click();
	await expect(main.getByTestId('approval-warning')).toContainText('already consumed');
	await expect(main.getByTestId('approval-decide')).toHaveCount(0);
	await main.getByTestId('approval-warning').getByRole('button', { name: 'Details' }).click();
	await expect(section.getByTestId('approval-error')).toContainText('already consumed');
	await expect(section.getByTestId('approval-spent')).toHaveCount(0);
	expect((await callsOf(page, 'decideApproval')).length).toBe(1);

	// A definitive refusal invalidates the main action even if a stale read
	// still says pending. Retain the receipt and error; never fabricate consume.
	await expect(section.getByTestId('approval-status')).toHaveText('pending');
	// The recorded approval was never consumed by a refused attempt.
	const stored = await page.evaluate(() => {
		const calls = window.__maiChatApi!.calls;
		return calls.filter((call) => call.op === 'readApproval').length >= 1;
	});
	expect(stored).toBe(true);
});

test('no bound decision record renders an honest absent state with the recorded denial', async ({
	page
}) => {
	await openFixture(page, 'approval-absent');
	const main = page.getByTestId('host-main');

	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	const section = main.getByTestId('approval-section');
	await expect(section).toBeHidden();
	await expect(main.getByTestId('approval-warning')).toContainText('no actionable bound decision');
	await expect(main.getByTestId('approval-decide')).toHaveCount(0);
	await main.getByTestId('approval-warning').getByRole('button', { name: 'Details' }).click();
	await expect(section.getByTestId('approval-absent')).toBeVisible();

	// The recorded denial of the un-approved action shows as a value-free
	// tool outcome (the producer's stable reason), not as a fake success.
	await queueStreamPair({
		frames: [
			frame(1, 'step.completed', {
				capability_id: 'fixture.exec',
				reason: 'approval_required_but_absent',
				op_id: OP_ID
			})
		]
	});
	const transcript = main.getByTestId('transcript-list');
	await expect(transcript.getByTestId('transcript-value')).toContainText([
		'approval_required_but_absent'
	]);
	await expect(section.getByTestId('approval-absent')).toBeVisible();
});

test('Details defaults closed and preserves draft, transcript, scope and subscriptions', async ({ page }) => {
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');
	const second = page.getByTestId('host-second');
	await submitFromHost(page, 'host-main', 'Original prompt');
	await waitForStreamRequests(2);
	queueStreamPair({ frames: [
		frame(1, 'agent.started', { model: 'fixture-model-mini', profile: 'fixture-fast' }),
		frame(2, 'agent.model_round', { round: 1, status: 'tool_calls', tool_calls: [{ seq: 1, capability_id: 'fixture.read' }] }),
		frame(3, 'agent.model_round', { round: 2, status: 'final', output: '**Verified answer**', output_chars: 19 })
	] });
	await waitForStreamRequests(4);
	await expect(main.getByTestId('assistant-reply-text').locator('strong')).toHaveText('Verified answer');
	await expect(main.getByTestId('assistant-tool-calls')).toBeVisible();
	await expect(main.getByTestId('assistant-thinking-block')).toBeVisible();
	await expect(main.getByTestId('conversation-message')).toHaveText('Original prompt');
	await expect(main.getByTestId('chat-details-trigger')).toHaveAttribute('aria-expanded', 'false');
	for (const id of ['conversation-children', 'conversation-transcript', 'conversation-attachments', 'conversation-target', 'conversation-id', 'conversation-refresh']) {
		await expect(main.getByTestId(id)).toBeHidden();
	}
	await expect(main.locator('p[data-testid="stream-status"]')).toBeHidden();
	await main.getByTestId('compose-message').fill('Unsent next question');
	await second.getByTestId('compose-message').fill('Independent draft');
	const before = await page.evaluate(() => window.__maiChatApi!.calls);
	const entries = await main.getByTestId('transcript-entry').allTextContents();
	await main.getByTestId('chat-details-trigger').click();
	await expect(main.getByTestId('conversation-children')).toBeVisible();
	await main.getByTestId('conversation-transcript').locator('summary').click();
	await expect(main.getByTestId('transcript-list')).toBeVisible();
	// The expanded panel covers the trigger (its ✕ is the close affordance);
	// reopening rides the trigger again once the panel is out of the way.
	await main.getByTestId('chat-details-close').click();
	await main.getByTestId('chat-details-trigger').click();
	await expect(main.getByTestId('transcript-list')).toBeVisible();
	await expect(main.getByTestId('compose-message')).toHaveValue('Unsent next question');
	await expect(second.getByTestId('compose-message')).toHaveValue('Independent draft');
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Alpha');
	expect(await main.getByTestId('transcript-entry').allTextContents()).toEqual(entries);
	expect(await page.evaluate(() => window.__maiChatApi!.calls)).toEqual(before);
	// Starting a new run resets disclosure state without changing sibling state.
	const originalRunId = (await callsOf(page, 'submitMessage'))[0].payload as { run_id: string };
	await main.getByTestId('chat-new-session').click();
	await submitFromHost(page, 'host-main', 'New prompt');
	await expect(main.getByTestId('chat-details-trigger')).toHaveAttribute('aria-expanded', 'false');
	await main.getByTestId('chat-details-trigger').click();
	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	await expect(main.getByTestId('chat-details-trigger')).toHaveAttribute('aria-expanded', 'false');
	await main.getByTestId('chat-details-trigger').click();
	await expect(main.getByTestId('conversation-id')).toHaveText(originalRunId.run_id.slice(0, 8));
	await expect(second.getByTestId('compose-message')).toHaveValue('Independent draft');
});

for (const state of ['succeeded', 'failed', 'cancelled']) {
	test(`terminal ${state} removes approval action but retains exact receipt in Details`, async ({ page }) => {
		await openFixture(page, 'approval');
		const main = page.getByTestId('host-main');
		await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
		await expect(main.getByTestId('approval-decide')).toBeVisible();
		await main.getByTestId('chat-details-trigger').click();
		await page.evaluate(({ runId, state }) => window.__maiChatApi!.setConversationState(runId, state), { runId: ROOT, state });
		await main.getByTestId('conversation-refresh').click();
		await expect(main.getByTestId('approval-decide')).toHaveCount(0);
		await expect(main.getByTestId('approval-id')).toHaveText(APPROVAL_ID);
		await main.getByTestId('chat-details-close').click();
		await expect(main.getByTestId('approval-section')).toBeHidden();
		await expect(main.getByTestId('approval-warning')).toHaveCount(0);
		expect(await callsOf(page, 'decideApproval')).toHaveLength(0);
	});
}

for (const fields of [
	{ status: 'expired' },
	{ status: 'consumed' },
	{ expires_at: '2026-10-16T09:00:00Z' },
	{ payload_hash: 'cd'.repeat(32) },
	{ server_id: SERVER_B },
	{ policy_revision: 99 }
]) {
	test(`non-actionable approval ${JSON.stringify(fields)} never offers a decision`, async ({ page }) => {
		await openFixture(page, 'approval');
		await page.evaluate((fields) => window.__maiChatApi!.setApprovalFields(fields), fields);
		const main = page.getByTestId('host-main');
		await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
		await expect.poll(async () => (await callsOf(page, 'readApproval')).length).toBeGreaterThan(0);
		await main.getByTestId('chat-details-trigger').click();
		await expect(main.getByTestId('approval-id')).toHaveText(APPROVAL_ID);
		await expect(main.getByTestId('approval-decide')).toHaveCount(0);
		expect(await callsOf(page, 'decideApproval')).toHaveLength(0);
	});
}

test('pending approval expires while open without a decision or automatic Details opening', async ({ page }) => {
	await openFixture(page, 'approval');
	await page.clock.install({ time: new Date('2026-10-16T09:30:00Z') });
	await page.evaluate(() => window.__maiChatApi!.setApprovalFields({ expires_at: '2026-10-16T09:30:02Z' }));
	const main = page.getByTestId('host-main');
	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	await expect(main.getByTestId('approval-decide')).toBeVisible();
	await page.clock.fastForward(3000);
	await expect(main.getByTestId('approval-decide')).toHaveCount(0);
	await expect(main.getByTestId('chat-details-panel')).toBeHidden();
	expect(await callsOf(page, 'decideApproval')).toHaveLength(0);
});

test('completed conversation keeps reply visible and normal closed stream and empty diagnostics hidden', async ({ page }) => {
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');
	await submitFromHost(page, 'host-main', 'Completed prompt');
	const body = (await callsOf(page, 'submitMessage'))[0].payload as { run_id: string };
	await page.evaluate((id) => window.__maiChatApi!.setConversationState(id, 'succeeded'), body.run_id);
	queueStreamPair({ frames: [
		frame(1, 'agent.model_round', { round: 1, status: 'final', output: 'Completed answer', output_chars: 16 }),
		frame(2, 'agent.completed', { rounds: 1, tool_calls: 0, output_chars: 16 })
	] });
	await expect(main.getByTestId('conversation-state')).toHaveText('Succeeded');
	await expect(main.getByTestId('assistant-reply-text')).toHaveText('Completed answer');
	await expect(main.locator('p[data-testid="stream-status"]')).toContainText('closed');
	for (const id of ['chat-details-panel', 'attachments-empty', 'conversation-children', 'conversation-transcript']) {
		await expect(main.getByTestId(id)).toBeHidden();
	}
	await expect(main.locator('p[data-testid="stream-status"]')).toBeHidden();
	await main.getByTestId('chat-details-trigger').click();
	await expect(main.getByTestId('attachments-empty')).toBeVisible();
	await expect(main.getByTestId('conversation-children')).toBeVisible();
});

test('approval load failure warns outside Details, never fabricates approval, and recovers on refresh', async ({ page }) => {
	await openFixture(page, 'approval');
	await page.evaluate(() => window.__maiChatApi!.failApprovalRead({ status: 503, code: 'unavailable', message: 'Fixture approval unavailable' }));
	const main = page.getByTestId('host-main');
	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	await expect(main.getByTestId('approval-warning')).toContainText('Fixture approval unavailable');
	await expect(main.getByTestId('chat-details-panel')).toBeHidden();
	await expect(main.getByTestId('approval-decide')).toHaveCount(0);
	await main.getByTestId('approval-warning').getByRole('button', { name: 'Details' }).click();
	await expect(main.getByTestId('approval-error')).toBeVisible();
	await page.evaluate(() => window.__maiChatApi!.failApprovalRead(null));
	await main.getByTestId('conversation-refresh').click();
	await expect(main.getByTestId('approval-decide')).toBeVisible();
	await expect(main.getByTestId('approval-warning')).toHaveCount(0);
});

test('stream and refresh failures remain visible with Details closed', async ({ page }) => {
	await openFixture(page, 'replay');
	const main = page.getByTestId('host-main');
	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	queueStreamPair({ abort: true });
	await expect(main.locator('.mai-chat__inline-alerts [data-testid="stream-problem"]')).toBeVisible();
	await expect(main.getByTestId('chat-details-panel')).toBeHidden();
	await waitForStreamRequests(4);
	await main.getByTestId('chat-details-trigger').click();
	await page.evaluate(() => window.__maiChatApi!.failNextRead({ status: 503, code: 'unavailable', message: 'Fixture refresh unavailable' }));
	await main.getByTestId('conversation-refresh').click();
	await expect(main.locator('.mai-chat__inline-alerts [data-testid="refresh-problem"]')).toContainText('Fixture refresh unavailable');
	await main.getByTestId('chat-details-close').click();
	await expect(main.locator('.mai-chat__inline-alerts [data-testid="refresh-problem"]')).toBeVisible();
	await expect(main.locator('p[data-testid="stream-status"]')).toBeHidden();
});

test('mount reads only; unmount submits nothing; sibling drives no traffic', async ({ page }) => {
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');

	// Boot: reads only.
	expect((await callsOf(page, 'submitMessage')).length).toBe(0);
	expect((await callsOf(page, 'cancelConversation')).length).toBe(0);
	expect((await callsOf(page, 'decideApproval')).length).toBe(0);

	// One deliberate submit, then close the window content: the teardown must
	// record no further mutation and release the subscriptions.
	await submitFromHost(page, 'host-main', 'Watch me close without side effects.');
	const mutationsAfterSubmit = await page.evaluate(() =>
		window.__maiChatApi!.calls.filter((call) =>
			['submitMessage', 'cancelConversation', 'decideApproval'].includes(call.op)
		).length
	);
	await page.evaluate(() => window.__maiChatApi!.toggleMain(false));
	await expect(main.getByTestId('chat-root')).toHaveCount(0);
	await page.evaluate(() => window.__maiChatApi!.toggleMain(true));
	await expect(main.getByTestId('chat-root')).toBeVisible();
	expect(
		await page.evaluate(
			() =>
				window.__maiChatApi!.calls.filter((call) =>
					['submitMessage', 'cancelConversation', 'decideApproval'].includes(call.op)
				).length
		)
	).toBe(mutationsAfterSubmit);

	// Reopen remounted the panel, which re-reads its scope — but still never
	// submits, cancels or decides on its own.
	await expect
		.poll(async () => (await callsOf(page, 'listConversations')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(3);

	// The benign sibling panel drives zero conversation traffic.
	const callsBefore = await page.evaluate(() => window.__maiChatApi!.calls.length);
	const siblingButton = page.getByTestId('host-sibling').getByTestId('sibling-interactions');
	await siblingButton.click();
	await expect(siblingButton).toHaveText('Sibling interactions: 1');
	expect(await page.evaluate(() => window.__maiChatApi!.calls.length)).toBe(callsBefore);
});

test('fail-closed without services; wrong scope refused; the feature entry validates in the registry', async ({
	page
}) => {
	await openFixture(page, 'compose');

	// BARE host: no services bag ⇒ visible config error, zero requests.
	const bare = page.getByTestId('host-bare');
	await expect(bare.getByTestId('panel-config-error')).toBeVisible();

	// GLOBAL host: a server-only entry is refused at the kind gate — the
	// panel body never mounts (C11 scope validation).
	const globalHost = page.getByTestId('host-global');
	await expect(globalHost.getByTestId('panel-unavailable')).toHaveAttribute(
		'data-reason',
		'scope_kind_not_allowed'
	);
	await expect(globalHost.getByTestId('chat-root')).toHaveCount(0);

	// Only the two usable hosts ever called; each with its own pinned scope.
	await expect
		.poll(async () => (await callsOf(page, 'listConversations')).map((call) => call.serverId).sort(), {
			timeout: 15000
		})
		.toEqual([SERVER_A, SERVER_B]);

	// The real registry knows the entry: server scopes are allowed, a pinned
	// CONVERSATION object is allowed (the panel composes conversations), a
	// foreign object pin is refused at the kind gate, and so is the global
	// scope; geometry is pinned by the entry.
	const probe = await page.evaluate((pin) => {
		const api = window.__maiChatApi!;
		return {
			kinds: api.registry.kinds,
			serverScope: api.registry.checkScope('chat', { kind: 'server', server_id: pin.server }),
			conversationPin: api.registry.checkScope('chat', {
				kind: 'server',
				server_id: pin.server,
				object: { kind: 'conversation', id: pin.run }
			}),
			foreignPin: api.registry.checkScope('chat', {
				kind: 'server',
				server_id: pin.server,
				object: { kind: 'run', id: pin.run }
			}),
			globalScope: api.registry.checkScope('chat', { kind: 'global' }),
			defaultGeometry: api.registry.defaultGeometry('chat'),
			minGeometry: api.registry.minGeometry('chat')
		};
	}, { server: SERVER_A, run: ROOT });
	expect(probe.kinds).toContain('chat');
	expect(probe.serverScope).toEqual({ ok: true });
	expect(probe.conversationPin).toEqual({ ok: true });
	expect(probe.foreignPin).toEqual({ ok: false, code: 'scope_object_kind_not_allowed' });
	expect(probe.globalScope).toEqual({ ok: false, code: 'scope_kind_not_allowed' });
	expect(probe.defaultGeometry).toEqual({ x: 48, y: 72, width: 780, height: 620 });
	expect(probe.minGeometry).toEqual({ x: 0, y: 0, width: 480, height: 360 });
});

test('compose validation stays local; a finished conversation replays without invented labels', async ({
	page
}) => {
	await openFixture(page, 'history');
	const main = page.getByTestId('host-main');

	// Empty compose: the local pre-flight blocks the request entirely.
	await main.getByTestId('chat-submit').click();
	await expect(main.getByText('A message is required.')).toBeVisible();
	expect((await callsOf(page, 'submitMessage')).length).toBe(0);

	// A malformed attachment reference is a local error too (no request);
	// the inline field error renders beside the composer attachments input.
	await main.getByTestId('compose-message').fill('Hostile attachments are refused locally.');
	await main.getByTestId('compose-profile').fill('fixture-fast');
	await main.getByTestId('compose-attachments').fill('not-a-uuid');
	await main.getByTestId('chat-submit').click();
	await expect(
		main.getByText('Attachment references must be canonical op ids (UUIDs).')
	).toBeVisible();
	expect((await callsOf(page, 'submitMessage')).length).toBe(0);

	// Open the recorded conversation: the wire carries no task bodies, so the
	// message reads as the honest unknown and the child renders as an id.
	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-open').first().click();
	await main.getByTestId('chat-details-trigger').click();
	await expect(main.getByTestId('conversation-id')).toBeVisible();
	await expect(main.getByTestId('conversation-message')).toHaveText(
		'Authored outside this window — the wire carries no task bodies.'
	);

	await page.evaluate((runId) =>
		window.__maiChatApi!.setConversationState(runId, 'succeeded'), HISTORY_RUN);
	await queueStreamPair({
		frames: [
			frame(1, 'chat.submitted', { submission_sha256: PAYLOAD_SHA, handler: 'agent.run', children: 0, attachments: [{}] }),
			frame(2, 'agent.started', {
				connection: 'fixture-gateway',
				profile: 'fixture-fast',
				model: 'fixture-model-mini',
				role: 'administrator',
				rounds_budget: 8
			}),
			frame(3, 'agent.model_round', { round: 2, status: 'final', output_chars: 42 }),
			frame(4, 'agent.completed', { rounds: 2, tool_calls: 1, output_chars: 42 })
		]
	});
	const transcript = main.getByTestId('transcript-list');
	await expect(transcript.getByTestId('transcript-entry')).toHaveCount(4);
	await expect(transcript.getByTestId('transcript-value')).toContainText([
		'held in the durable run history — the wire carries no reply text',
		'42'
	]);
	await expect(main.getByTestId('conversation-state')).toHaveText('Succeeded');

	// The tree shows the child identity; no invented task label exists.
	const tree = main.getByTestId('conversation-children');
	await expect(tree.getByText(CHILD_A.slice(0, 8))).toBeVisible();
	await expect(tree.getByText('aspect-alpha review')).toHaveCount(0);
});

test('draft Server Chip: remove → General Consult, re-pick, @service chips; everything locks once the chat starts', async ({
	page
}) => {
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');

	// Draft: the chip reflects the pinned scope and is removable.
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Alpha');
	await expect(main.getByTestId('chat-server-chip')).not.toHaveAttribute('data-locked', 'true');

	// Removing it switches the composer to General Consult mode.
	await main.getByTestId('chat-server-chip-remove').click();
	await expect(main.getByTestId('chat-server-chip')).toHaveCount(0);
	await expect(main.getByTestId('chat-general-consult-badge')).toHaveText(
		'General Consult — No Server Pinned'
	);

	// Re-picking a server from the draft picker restores the chip.
	await main.getByTestId('chat-server-picker').selectOption({ label: 'Server Beta' });
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Beta');

	// `@service` chips: the toggle opens the autocomplete…
	await main.getByTestId('chat-service-picker-toggle').click();
	await expect(main.getByTestId('chat-service-autocomplete')).toBeVisible();
	await main
		.getByTestId('chat-service-option')
		.filter({ hasText: '@enshrouded' })
		.click();
	await expect(main.getByTestId('chat-service-chip')).toHaveCount(1);
	await expect(main.getByTestId('chat-service-chip')).toContainText('@enshrouded');

	// …and typing `@<query>` in the message input opens the filtered list.
	await main.getByTestId('compose-message').fill('check @ng');
	await expect(main.getByTestId('chat-service-autocomplete')).toBeVisible();
	await main
		.getByTestId('chat-service-option')
		.filter({ hasText: '@nginx' })
		.click();
	await expect(main.getByTestId('chat-service-chip')).toHaveCount(2);
	// The consumed `@<token>` left the draft text.
	await expect(main.getByTestId('compose-message')).toHaveValue('check ');

	// A chip is removable while still in draft (drop the @nginx probe chip).
	await main
		.getByTestId('chat-service-chip')
		.filter({ hasText: '@nginx' })
		.getByTestId('chat-service-chip-remove')
		.click();
	await expect(main.getByTestId('chat-service-chip')).toHaveCount(1);

	// Submit: server_id carries the picked server and the body records the
	// remaining `@service` chip.
	await main.getByTestId('compose-message').fill('Consult the pinned services.');
	await main.getByTestId('compose-profile').fill('fixture-fast');
	await main.getByTestId('chat-submit').click();
	await expect(main.getByTestId('conversation-state')).toHaveText('Running');
	await expect
		.poll(async () => (await callsOf(page, 'submitMessage')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(1);
	const submissions = await callsOf(page, 'submitMessage');
	const body = submissions[submissions.length - 1].payload as Record<string, unknown>;
	expect(body.server_id).toBe(SERVER_B);
	expect(body.service_tags).toEqual(['enshrouded']);

	// Immutable once started: chips lock, remove/pick controls leave the DOM.
	await expect(main.getByTestId('chat-server-chip')).toHaveAttribute('data-locked', 'true');
	await expect(main.getByTestId('chat-service-chip')).toHaveAttribute('data-locked', 'true');
	await expect(main.getByTestId('chat-context-locked')).toHaveCount(0);
	await expect(main.getByTestId('chat-server-chip-remove')).toHaveCount(0);
	await expect(main.getByTestId('chat-service-chip-remove')).toHaveCount(0);
	await expect(main.getByTestId('chat-server-picker')).toHaveCount(0);
	await expect(main.getByTestId('chat-service-picker-toggle')).toHaveCount(0);

	// `New Chat` resets to a fresh unlocked draft with the default chip.
	await main.getByTestId('chat-new-session').click();
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Alpha');
	await expect(main.getByTestId('chat-server-chip')).not.toHaveAttribute('data-locked', 'true');
	await expect(main.getByTestId('chat-server-chip-remove')).toBeVisible();
	await expect(main.getByTestId('chat-service-chip')).toHaveCount(0);
	await expect(main.getByTestId('chat-context-locked')).toHaveCount(0);
	await expect(main.getByTestId('chat-server-picker')).toBeVisible();
});

test('General Consult submit rides the sentinel server id and restores as the consult badge', async ({
	page
}) => {
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');

	// Remove the Server Chip → General Consult mode.
	await main.getByTestId('chat-server-chip-remove').click();
	await expect(main.getByTestId('chat-general-consult-badge')).toBeVisible();

	await main.getByTestId('compose-message').fill('Cross-server consult question.');
	await main.getByTestId('compose-profile').fill('fixture-fast');
	await main.getByTestId('chat-submit').click();
	await expect
		.poll(async () => (await callsOf(page, 'submitMessage')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(1);
	const submissions = await callsOf(page, 'submitMessage');
	const body = submissions[0].payload as Record<string, unknown>;
	expect(body.server_id).toBe(GLOBAL_CONSULT_SERVER_ID);

	// The recorded consult conversation shows the badge — never a Server Chip —
	// and it is locked.
	await expect(main.getByTestId('chat-general-consult-badge')).toBeVisible();
	await expect(main.getByTestId('chat-server-chip')).toHaveCount(0);
	await expect(main.getByTestId('chat-context-locked')).toHaveCount(0);

	// New Chat restores the default Server Chip.
	await main.getByTestId('chat-new-session').click();
	await expect(main.getByTestId('chat-general-consult-badge')).toHaveCount(0);
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Alpha');
});

test('the floating chat history selector restores the transcript and the locked server/service context', async ({
	page
}) => {
	await openFixture(page, 'history');
	const main = page.getByTestId('host-main');

	// The header toggle opens the floating popover with the recorded session.
	await main.getByTestId('chat-history-toggle').click();
	const popover = main.getByTestId('chat-history-popover');
	await expect(popover).toBeVisible();
	const item = popover.getByTestId('chat-history-item');
	await expect(item).toHaveCount(1);
	await expect(item).toContainText('Server Alpha');
	await expect(item).toContainText('@enshrouded');
	await expect(item).toContainText('@nginx');
	await expect(item).toContainText('Running');
	await expect(item).toContainText('2026-10-16T09:00:00Z');

	// Selecting the session loads its transcript AND restores its frozen
	// context in the locked state.
	await item.click();
	await expect(main.getByTestId('chat-history-popover')).toHaveCount(0);
	await expect(main.getByTestId('chat-details-panel')).toBeHidden();
	await main.getByTestId('chat-details-trigger').click();
	await expect(main.getByTestId('conversation-id')).toHaveText(HISTORY_RUN.slice(0, 8));
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Alpha');
	await expect(main.getByTestId('chat-server-chip')).toHaveAttribute('data-locked', 'true');
	await expect(main.getByTestId('chat-service-chip')).toHaveCount(2);
	await expect(main.getByTestId('chat-context-locked')).toHaveCount(0);
	await expect(main.getByTestId('compose-profile')).toHaveValue('fixture-fast');
	await expect(main.getByTestId('chat-server-chip-remove')).toHaveCount(0);
	await expect(main.getByTestId('chat-service-chip-remove')).toHaveCount(0);
	await expect(main.getByTestId('chat-server-picker')).toHaveCount(0);
	await expect(main.getByTestId('chat-service-picker-toggle')).toHaveCount(0);

	// `New Chat` unlocks and returns to the default draft.
	await main.getByTestId('chat-new-session').click();
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Alpha');
	await expect(main.getByTestId('chat-server-chip-remove')).toBeVisible();
	await expect(main.getByTestId('chat-service-chip')).toHaveCount(0);
});

test('the mai:chat-focus-service window event pre-populates a new draft and never mutates a locked one', async ({
	page
}) => {
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');

	// A draft draft accepts the Service Map seam event.
	await page.evaluate(
		([serverId, serviceName, category]) => {
			window.dispatchEvent(
				new CustomEvent('mai:chat-focus-service', {
					detail: { serverId, serviceName, category }
				})
			);
		},
		[SERVER_B, 'enshrouded', 'games'] as const
	);
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Beta');
	await expect(main.getByTestId('chat-service-chip')).toContainText('@enshrouded');

	// Start the chat; the recorded conversation locks to its scope.
	await main.getByTestId('compose-message').fill('Seam-started conversation.');
	await main.getByTestId('compose-profile').fill('fixture-fast');
	await main.getByTestId('chat-submit').click();
	await expect
		.poll(async () => (await callsOf(page, 'submitMessage')).length, { timeout: 15000 })
		.toBeGreaterThanOrEqual(1);
	const submissions = await callsOf(page, 'submitMessage');
	const body = submissions[0].payload as Record<string, unknown>;
	expect(body.server_id).toBe(SERVER_B);
	expect(body.service_tags).toEqual(['enshrouded']);

	// A locked session ignores a later focus-service event entirely.
	await page.evaluate(
		([serverId, serviceName]) => {
			window.dispatchEvent(
				new CustomEvent('mai:chat-focus-service', {
					detail: { serverId, serviceName, category: 'web' }
				})
			);
		},
		[SERVER_A, 'nginx'] as const
	);
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Beta');
	await expect(main.getByTestId('chat-service-chip')).toHaveCount(1);

	// New Chat unlocks again.
	await main.getByTestId('chat-new-session').click();
	await expect(main.getByTestId('chat-server-chip')).toContainText('Server Alpha');
	await expect(main.getByTestId('chat-service-chip')).toHaveCount(0);
});

for (const viewport of [{ width: 1280, height: 800 }, { width: 360, height: 640 }]) {
	for (const framed of [false, true]) {
	test(`chat layout ${viewport.width} framed=${framed}: full width, bounded scrolling and reversible controls`, async ({ page }) => {
		await page.setViewportSize(viewport);
		await page.addInitScript((value) => { window.__maiChatFramed = value; }, framed);
		await openFixture(page, 'compose');
		// Isolate a single standalone host, without overriding any ChatPanel style.
		await page.addStyleTag({ content: 'body { margin: 0; } #app > section:not([data-testid="host-main"]) { display:none; }' });
		const main = page.getByTestId('host-main');
		await expect(main.getByTestId('compose-profile')).toBeVisible();
		await expect(main.getByTestId('compose-read-only-toggle')).toBeVisible();
		await expect(main.getByTestId('chat-history')).toHaveCount(0);
		const longText = 'LongWord'.repeat(100) + '\n' + 'Full width paragraph. '.repeat(150);
		await submitFromHost(page, 'host-main', longText);
		await queueStreamPair({ frames: [
			frame(1, 'agent.started', { model: 'fixture-model', profile: 'fixture-fast' }),
			frame(2, 'agent.model_round', { round: 1, status: 'tool_calls', tool_calls: [{seq: 1, capability_id: 'fixture.read', payload_hash: PAYLOAD_SHA}] }),
			frame(3, 'agent.model_round', { round: 2, status: 'final', output: '**Readable answer**\n\n' + longText + '\n\n```text\n' + 'code'.repeat(200) + '\n```' })
		] });
		await expect(main.getByTestId('assistant-reply-text').locator('strong')).toHaveText('Readable answer');
		await expect(main.getByTestId('assistant-reply-text').locator('pre')).toContainText('code'.repeat(200));
		await expect(main.getByTestId('assistant-tool-card')).toContainText('fixture.read');
		await expect(main.getByTestId('chat-details-trigger')).toHaveAttribute('aria-expanded', 'false');
		const draft = main.getByTestId('compose-message');
		await draft.fill('Unsent draft survives both disclosures');
		await waitForStreamRequests(4);
		await expect.poll(async () => (await callsOf(page, 'readConversation')).length).toBeGreaterThanOrEqual(3);
		const callsBefore = await page.evaluate(() => window.__maiChatApi!.calls);
		async function geometry() {
			const result = await main.evaluate((host) => {
				const get = (selector: string) => host.querySelector(selector)! as HTMLElement;
				const rect = (selector: string) => { const r = get(selector).getBoundingClientRect(); return {x:r.x, y:r.y, right:r.right, bottom:r.bottom, width:r.width, height:r.height}; };
				const scroll = get('.mai-chat__conversation-scroll');
				const bubble = get('.mai-chat__bubble');
				return {
					conversation: rect('[data-testid="chat-conversation"]'), composer: rect('[data-testid="chat-compose"]'),
					input: rect('[data-testid="compose-message"]'), send: rect('[data-testid="chat-submit"]'),
					trigger: rect('[data-testid="chat-details-trigger"]'),
					scrollHeight: scroll.scrollHeight, clientHeight: scroll.clientHeight,
					scrollWidth: scroll.clientWidth, bubbleWidth: bubble.getBoundingClientRect().width,
					border: getComputedStyle(bubble).borderTopWidth, shadow: getComputedStyle(bubble).boxShadow,
					pageWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth,
					body: rect('[data-testid="chat-root"]')
				};
			});
			expect(result.pageWidth).toBeLessThanOrEqual(result.viewportWidth);
			expect(result.input.bottom).toBeLessThanOrEqual(viewport.height);
			expect(result.send.bottom).toBeLessThanOrEqual(viewport.height);
			expect(result.input.y).toBeGreaterThanOrEqual(0);
			expect(result.send.right).toBeLessThanOrEqual(viewport.width);
			expect(result.conversation.bottom).toBeLessThanOrEqual(result.composer.y);
			expect(result.trigger.y).toBeLessThan(result.composer.y);
			expect(result.scrollHeight).toBeGreaterThan(result.clientHeight);
			expect(result.clientHeight).toBeGreaterThan(24);
			expect(result.bubbleWidth).toBeGreaterThanOrEqual(result.scrollWidth - 1);
			expect(result.border).toBe('0px'); expect(result.shadow).toBe('none');
			expect(result.composer.bottom).toBeCloseTo(result.body.bottom, 0);
		}
		await geometry();
		// The read-only toggle is part of the always-visible composer bar; it
		// is exercised while the details sheet is CLOSED (the open sheet
		// overlays the composer and its ✕ is the close affordance).
		await main.getByTestId('compose-read-only-toggle').click();
		await geometry();
		await main.getByTestId('compose-read-only-toggle').click();
		await main.getByTestId('chat-details-trigger').click();
		await main.getByTestId('conversation-transcript').locator('summary').click();
		await geometry();
		await main.getByTestId('conversation-refresh').focus();
		await page.keyboard.press('Escape');
		await expect(main.getByTestId('chat-details-trigger')).toBeFocused();
		await expect(main.getByTestId('chat-details-panel')).toBeHidden();
		await expect(draft).toHaveValue('Unsent draft survives both disclosures');
		await geometry();
		expect(await page.evaluate(() => window.__maiChatApi!.calls)).toEqual(callsBefore);
	});

	}
	test(`pending approval ${viewport.width} remains reachable outside Details`, async ({page}) => {
		await page.setViewportSize(viewport);
		await openFixture(page, 'approval');
		await page.addStyleTag({ content: 'body { margin:0; } #app > section:not([data-testid="host-main"]) { display:none; }' });
		const main = page.getByTestId('host-main');
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-open').click();
		await expect(main.getByTestId('approval-decide')).toBeVisible();
		await expect(main.getByTestId('chat-details-panel')).toBeHidden();
		await main.getByTestId('approval-decide').focus();
		const bounds = await main.getByTestId('approval-decide').boundingBox();
		expect(bounds!.y).toBeGreaterThanOrEqual(0);
		expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height);
		await main.getByTestId('chat-details-trigger').click();
		await expect(main.getByTestId('approval-hash')).toHaveText(PAYLOAD_SHA);
		await expect(main.getByTestId('approval-server')).toHaveText(SERVER_A);
		const send = await main.getByTestId('chat-submit').boundingBox();
		expect(send!.y + send!.height).toBeLessThanOrEqual(viewport.height);
		expect(await callsOf(page, 'decideApproval')).toHaveLength(0);
	});
}

test('profile validation stays inline and blocks submission without a request', async ({page}) => {
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');
	await main.getByTestId('compose-profile').fill('');
	await main.getByTestId('compose-message').fill('Message without a required profile');
	await main.getByTestId('chat-submit').click();
	await expect(main.getByText('A model profile is required.')).toBeVisible();
	await expect(main.getByTestId('compose-profile')).toBeVisible();
	await expect(main.getByTestId('compose-message')).toHaveValue('Message without a required profile');
	expect(await callsOf(page, 'submitMessage')).toEqual([]);
});

test('live model profiles expose ONLY the configured profiles and auto-select the stored default', async ({
	page
}) => {
	// The real wire reads: GET /api/v1/models/profiles + /models/selection.
	await page.route('**/api/v1/models/profiles', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				profiles: [
					{ name: 'alpha-live', revision: 2 },
					{ name: 'beta-live', revision: 1 }
				]
			})
		})
	);
	await page.route('**/api/v1/models/selection', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ connection: 'alpha', profile: 'beta-live' })
		})
	);
	await page.addInitScript((value) => {
		window.__maiChatLive = value;
	}, true);
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');

	// The stored default selection auto-selects (never the fake 'default').
	await expect(main.getByTestId('compose-profile')).toHaveValue('beta-live');

	// The dropdown lists ONLY the configured profiles — no merged static
	// fixture vocabulary ('default', 'fast', 'reasoning', 'fixture-fast').
	const options = main
		.getByTestId('compose-profile-select')
		.locator('option');
	await expect(options).toHaveText(['Select profile…', 'beta-live', 'alpha-live']);

	// An unknown current value re-points at the stored default (the submit
	// body would never carry a profile the backend does not store).
	await main.getByTestId('compose-profile').fill('fixture-fast');
	await expect(main.getByTestId('compose-profile')).toHaveValue('beta-live');
});

test('chat history limits initial page to 10 conversations and loads more on demand', async ({
	page
}) => {
	await openFixture(page, 'compose');
	const main = page.getByTestId('host-main');

	// Seed 14 conversations on SERVER_A and reload history.
	await page.evaluate((serverId) => {
		window.__maiChatApi!.seedConversations(serverId, 14);
	}, SERVER_A);

	await main.getByTestId('chat-history-toggle').click();
	await main.getByTestId('history-refresh').click();

	await expect(main.getByTestId('history-row')).toHaveCount(10);
	await expect(main.getByTestId('history-total')).toHaveText(
		'Showing 10 of 14 recorded conversations.'
	);
	const loadMoreBtn = main.getByTestId('history-load-more');
	await expect(loadMoreBtn).toBeVisible();
	await expect(loadMoreBtn).toContainText('Load more (10 of 14)');

	await loadMoreBtn.click();
	await expect(main.getByTestId('history-row')).toHaveCount(14);
	await expect(main.getByTestId('history-total')).toHaveText(
		'Showing 14 of 14 recorded conversations.'
	);
	await expect(main.getByTestId('history-load-more')).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// Canonical conversation continuity (§3; task 10) — the in-page canonical
// store scenario. The panel runs its canonical-first behavior: raw-message
// submits (C1/C6), server-truth restore + bounded transcript paging (C2),
// the deterministic control plane (C5), gap honesty (C7) and scope
// isolation (C8). Legacy scenarios above keep the legacy-only bag and prove
// the graceful fallback.
// ---------------------------------------------------------------------------

test.describe('canonical conversation continuity (in-page canonical store)', () => {
	test('C1/C6: submits carry ONLY the raw message; a stale revision fails closed with nothing applied', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		await main.getByTestId('compose-message').fill('Plan only; do not change files');
		await main.getByTestId('compose-profile').fill('fixture-fast');
		await main.getByTestId('chat-submit').click();
		const creates = await callsOf(page, 'createConversation');
		expect(creates).toHaveLength(1);
		const body = creates[0].payload as Record<string, unknown>;
		// The wire carries ONLY the actual user text — never a stitched
		// "## Prior conversation turns" wrapper (C1/C6).
		expect(body.message).toBe('Plan only; do not change files');
		expect(body.server_id).toBe(SERVER_A);
		expect(body.profile).toBe('fixture-fast');
		expect(UUID_RE.test(String(body.client_request_id))).toBe(true);
		expect(body.task_action).toBe('continue');

		// The admitted turn is durable server-side: the transcript row is
		// the server's copy, rendered verbatim.
		await expect(main.getByTestId('canonical-user-message')).toContainText(
			'Plan only; do not change files'
		);

		// The trusted producer appends the reply; the run finishes and the
		// terminal hand-over pulls the produced rows into the transcript.
		const conversationId = await page.evaluate(() => window.__maiChatApi!.lastCanonicalId());
		expect(conversationId).not.toBeNull();
		await page.evaluate((id) => {
			window.__maiChatApi!.appendAssistant(id as string, 'done');
			window.__maiChatApi!.completeRun(id as string);
		}, conversationId);
		await expect(main.getByTestId('canonical-assistant-message')).toContainText('done');
		await expect(main.getByTestId('conversation-state')).toHaveText('Succeeded');

		// A follow-up rides the conversation id + revision pin, raw text only.
		await main.getByTestId('compose-message').fill('continue');
		await main.getByTestId('chat-submit').click();
		const follows = await callsOf(page, 'submitConversationMessage');
		expect(follows).toHaveLength(1);
		expect(follows[0].runId).toBe(conversationId);
		const followBody = follows[0].payload as Record<string, unknown>;
		expect(followBody.message).toBe('continue');
		expect(followBody.expected_revision).toBe(1);
		expect(followBody.task_action).toBe('continue');
		expect(followBody.server_id).toBe(SERVER_A);

		// C6: another writer moves the revision; the stale pin fails closed
		// with a structured conflict, the draft survives and nothing applied.
		await page.evaluate(() => window.__maiChatApi!.armRevision(2));
		await main.getByTestId('compose-message').fill('Stale follow-up');
		await main.getByTestId('chat-submit').click();
		const conflict = main.getByTestId('compose-error');
		await expect(conflict).toBeVisible();
		await expect(conflict).toHaveAttribute('data-error-code', 'revision_conflict');
		await expect(conflict).toContainText('NOT applied');
		await expect(main.getByTestId('compose-message')).toHaveValue('Stale follow-up');
		const userRows = await main.getByTestId('canonical-user-message').allTextContents();
		expect(userRows.filter((text) => text.includes('Stale follow-up'))).toHaveLength(0);
	});

	test('C2: history selection restores the server transcript with bounded paging and no local cache', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		await main.getByTestId('chat-history-toggle').click();
		await expect(main.getByTestId('history-row')).toHaveCount(2);
		await main.getByTestId('chat-history-item').first().click();

		// Restored strictly from the server reads (getConversation +
		// listConversationMessages) — the newest bounded window first.
		await expect(main.getByTestId('conversation-state')).toBeVisible();
		await expect(main.getByTestId('transcript-window-meta')).toHaveText(
			'Showing 10 of 14 recorded messages.'
		);
		await expect(main.getByTestId('canonical-user-message')).toHaveCount(5);
		await expect(main.getByTestId('canonical-assistant-message')).toHaveCount(5);
		await expect(main.getByTestId('canonical-user-message').last()).toContainText(
			'Restore question 7'
		);
		await expect(main.getByTestId('transcript-load-earlier')).toContainText(
			'Load earlier messages (10 of 14)'
		);

		// Bounded scroll container (AGENTS §1): the transcript scrolls, it
		// never grows the page.
		await expect(main.locator('.mai-chat__conversation-scroll')).toHaveCSS(
			'overflow-y',
			'auto'
		);

		// Load earlier: deduplicated prepend until the window covers all.
		await main.getByTestId('transcript-load-earlier').click();
		await expect(main.getByTestId('transcript-window-meta')).toHaveText(
			'Showing 14 of 14 recorded messages.'
		);
		await expect(main.getByTestId('canonical-user-message')).toHaveCount(7);
		await expect(main.getByTestId('canonical-assistant-message')).toHaveCount(7);
		await expect(main.getByTestId('transcript-load-earlier')).toHaveCount(0);
		await expect(main.getByTestId('canonical-user-message').first()).toContainText(
			'Restore question 1'
		);

		// No browser cache: the canonical restore writes NOTHING into web
		// storage — a fresh session re-reads the same server truth.
		const storage = await page.evaluate(() => ({
			authored: window.localStorage.getItem('mai.chat.authored.v1'),
			turns: window.localStorage.getItem('mai.chat.prior_turns.v1'),
			superseded: window.localStorage.getItem('mai.chat.superseded.v1')
		}));
		expect(storage).toEqual({ authored: null, turns: null, superseded: null });
		const reads = await callsOf(page, 'listConversationMessages');
		expect(reads.length).toBeGreaterThanOrEqual(2);
	});

	test('C5: steering, pause and cancel ride the deterministic control plane with honest queued states', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		await main.getByTestId('compose-message').fill('Long inspection task');
		await main.getByTestId('compose-profile').fill('fixture-fast');
		await main.getByTestId('chat-submit').click();
		await expect(main.getByTestId('canonical-user-message')).toContainText(
			'Long inspection task'
		);

		// Active lease: the follow-up is admitted, persisted, and honestly
		// reported queued_followup (202 semantics — no second executor).
		await page.evaluate(() => window.__maiChatApi!.armBusy(true));
		await main.getByTestId('compose-message').fill('Steer towards the nginx unit logs');
		await main.getByTestId('chat-submit').click();
		await expect(main.getByTestId('turn-queued-note')).toBeVisible();
		await expect(main.getByTestId('turn-queued-note')).toContainText(
			'queued behind the active run'
		);
		await expect(main.getByTestId('canonical-user-message').last()).toContainText(
			'Steer towards the nginx unit logs'
		);

		// Pause: a deterministic control row with honest pending delivery.
		await main.getByTestId('chat-pause').click();
		await expect(main.getByTestId('control-note')).toContainText('Pause recorded');
		const steers = await callsOf(page, 'steerConversation');
		expect(steers).toHaveLength(1);
		expect(steers[0].payload).toMatchObject({ action: 'pause' });
		await expect(main.getByTestId('pending-steering-note')).toContainText(
			'Pending steering rows: 1'
		);

		// Cancel through the same control plane; the recorded run state is
		// rendered as the verified truth.
		await main.getByTestId('chat-stop').click();
		await expect(main.getByTestId('control-note')).toContainText('Cancellation recorded');
		await expect(main.getByTestId('conversation-state')).toHaveText('Cancelled (verified)');
		const cancels = await callsOf(page, 'steerConversation');
		expect(cancels).toHaveLength(2);
		expect(cancels[1].payload).toMatchObject({ action: 'cancel' });
	});

	test('C7: partial legacy history renders the gap banner and refuses mutations honestly', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('chat-history-item').nth(1).click();
		const banner = main.getByTestId('legacy-gaps-banner');
		await expect(banner).toBeVisible();
		await expect(banner).toContainText('partial');
		await expect(main.getByTestId('legacy-gap')).toContainText('Sequences 2–5');
		await expect(main.getByTestId('canonical-user-message')).toContainText(
			'Legacy-era question'
		);

		await main.getByTestId('compose-message').fill('Mutate despite the gap');
		await main.getByTestId('chat-submit').click();
		const refusal = main.getByTestId('compose-error');
		await expect(refusal).toBeVisible();
		await expect(refusal).toHaveAttribute('data-error-code', 'history_reconstruction_incomplete');
		await expect(refusal).toContainText('scope confirmation');
		// Nothing was applied: the transcript still holds only the two rows.
		await expect(main.getByTestId('canonical-user-message')).toHaveCount(1);
		await expect(main.getByTestId('canonical-assistant-message')).toHaveCount(1);
	});

	test('C8: canonical scope isolation — other servers never render and cross-scope probes fail closed', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const second = page.getByTestId('host-second');

		// SERVER_B's panel sees none of SERVER_A's conversations.
		await second.getByTestId('chat-history-toggle').click();
		await expect(second.getByTestId('history-empty')).toBeVisible();
		await expect(second.getByTestId('history-row')).toHaveCount(0);

		// A direct cross-scope read returns ONLY the structured code.
		const probe = await page.evaluate(() => window.__maiChatApi!.probeCrossServer());
		expect(probe.code).toBe('not_found');
	});

	test('canonical history list pages with Load more inside a bounded container', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		await page.evaluate(() => window.__maiChatApi!.seedCanonicalConversations(11));
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();
		await expect(main.getByTestId('history-row')).toHaveCount(10);
		await expect(main.getByTestId('history-total')).toHaveText(
			'Showing 10 of 13 recorded conversations.'
		);
		await main.getByTestId('history-load-more').click();
		await expect(main.getByTestId('history-row')).toHaveCount(13);
		await expect(main.getByTestId('history-total')).toHaveText(
			'Showing 13 of 13 recorded conversations.'
		);
		await expect(main.getByTestId('history-load-more')).toHaveCount(0);
	});

	// --- 24a D-1: the canonical read scope follows the draft/recorded target -

	test('D-1: unpinned draft retargets EVERY canonical read to the Global sentinel — list, create, tail, messages, control — and the consult lands in history', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		// One pre-existing Global conversation the pinned scope must NOT see.
		await page.evaluate(
			([sentinel, conversationId]) =>
				window.__maiChatApi!.seedCanonicalRow(sentinel, conversationId, 'Pre-existing consult'),
			[GLOBAL_CONSULT_SERVER_ID, GLOBAL_CONV]
		);
		// The calls log is SHARED with host-second (pinned to SERVER_B):
		// baseline it so the audit below covers only THIS host's reads.
		const listsBefore = (await callsOf(page, 'listCanonicalConversations')).length;

		// Remove the server chip → Global draft.
		await main.getByTestId('chat-server-chip-remove').click();
		await expect(main.getByTestId('chat-general-consult-badge')).toBeVisible();

		// History refresh rides the sentinel: the pinned server's recorded
		// rows (Restore me / Partial legacy import) are invisible; only the
		// Global row renders.
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();
		await expect(main.getByTestId('history-row')).toHaveCount(1);
		await expect(main.getByTestId('history-row').first()).toContainText('Pre-existing consult');
		const lists = await callsOf(page, 'listCanonicalConversations');
		expect(lists.length).toBeGreaterThan(listsBefore);
		for (const call of lists.slice(listsBefore)) {
			expect(call.serverId).toBe(GLOBAL_CONSULT_SERVER_ID);
		}

		// Submit: the create names the sentinel — never the panel pin.
		await main.getByTestId('compose-message').fill('Cross-server consult question.');
		await main.getByTestId('compose-profile').fill('fixture-fast');
		await main.getByTestId('chat-submit').click();
		await expect(main.getByTestId('canonical-user-message').last()).toContainText(
			'Cross-server consult question.'
		);
		const creates = await callsOf(page, 'createConversation');
		expect(creates).toHaveLength(1);
		expect((creates[0].payload as Record<string, unknown>).server_id).toBe(
			GLOBAL_CONSULT_SERVER_ID
		);
		const conversationId = await page.evaluate(() => window.__maiChatApi!.lastCanonicalId());
		expect(conversationId).not.toBeNull();

		// While the run holds the lease, steering rides the sentinel too.
		await main.getByTestId('chat-pause').click();
		await expect(main.getByTestId('control-note')).toContainText('Pause recorded');
		const steers = await callsOf(page, 'steerConversation');
		expect(steers).toHaveLength(1);
		expect((steers[0].payload as Record<string, unknown>).server_id).toBe(
			GLOBAL_CONSULT_SERVER_ID
		);

		// Producer appends the reply and releases the lease; the terminal
		// hand-over pulls it in — every tail/message read on the sentinel,
		// no scope-blind 404 anywhere.
		await page.evaluate((id) => {
			window.__maiChatApi!.appendAssistant(id as string, 'consult answer');
			window.__maiChatApi!.completeRun(id as string);
		}, conversationId);
		await expect(main.getByTestId('canonical-assistant-message').last()).toContainText(
			'consult answer'
		);
		await expect(main.getByTestId('conversation-state')).toHaveText('Succeeded');
		await expect(main.getByTestId('canonical-problem')).toHaveCount(0);
		for (const call of await callsOf(page, 'listConversationEvents')) {
			expect(call.serverId).toBe(GLOBAL_CONSULT_SERVER_ID);
		}
		for (const call of await callsOf(page, 'listConversationMessages')) {
			expect(call.serverId).toBe(GLOBAL_CONSULT_SERVER_ID);
		}

		// The post-submit history refresh is ALSO Global-scoped: the created
		// consult appears next to the pre-existing one (V6 saw "Chat History 0").
		await expect(main.getByTestId('history-row')).toHaveCount(2);
		await expect(main.getByTestId('history-row')).toContainText([
			'Pre-existing consult',
			String(conversationId).slice(0, 8)
		]);
	});

	test('D-1: a Global history row restores and reopens on its RECORDED sentinel scope', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');
		await page.evaluate(
			([sentinel, conversationId]) =>
				window.__maiChatApi!.seedCanonicalRow(sentinel, conversationId, 'Recorded consult'),
			[GLOBAL_CONSULT_SERVER_ID, GLOBAL_CONV]
		);

		// Default draft (pinned server): the Global row is invisible.
		await main.getByTestId('chat-history-toggle').click();
		await expect(main.getByTestId('history-row')).toHaveCount(2);

		// Unpin → the Global row appears → restore it.
		await main.getByTestId('chat-server-chip-remove').click();
		await main.getByTestId('history-refresh').click();
		await expect(main.getByTestId('history-row')).toHaveCount(1);
		await main.getByTestId('chat-history-item').first().click();

		// The restore read the ROW's recorded sentinel — not the previous
		// panel pin (the pre-fix behavior 404'd this exact read).
		const gets = await callsOf(page, 'getConversation');
		expect(gets).toHaveLength(1);
		expect(gets[0].runId).toBe(GLOBAL_CONV);
		expect(gets[0].serverId).toBe(GLOBAL_CONSULT_SERVER_ID);
		await expect(main.getByTestId('canonical-user-message')).toContainText(
			'Seeded question — Recorded consult'
		);
		await expect(main.getByTestId('canonical-assistant-message')).toContainText(
			'Seeded recorded answer — Recorded consult'
		);
		await expect(main.getByTestId('chat-general-consult-badge')).toBeVisible();
		await expect(main.getByTestId('canonical-problem')).toHaveCount(0);
		for (const call of await callsOf(page, 'listConversationMessages')) {
			expect(call.serverId).toBe(GLOBAL_CONSULT_SERVER_ID);
		}

		// Reopen: New Chat resets the draft to the panel scope; unpin again
		// and restore the same row — server truth, role-preserving, no
		// duplicated rows.
		await main.getByTestId('chat-new-session').click();
		await expect(main.getByTestId('chat-server-chip')).toBeVisible();
		await main.getByTestId('chat-server-chip-remove').click();
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();
		await expect(main.getByTestId('history-row')).toHaveCount(1);
		await main.getByTestId('chat-history-item').first().click();
		await expect(main.getByTestId('canonical-user-message')).toHaveCount(1);
		await expect(main.getByTestId('canonical-assistant-message')).toHaveCount(1);
		const regets = await callsOf(page, 'getConversation');
		expect(regets).toHaveLength(2);
		for (const call of regets) {
			expect(call.runId).toBe(GLOBAL_CONV);
			expect(call.serverId).toBe(GLOBAL_CONSULT_SERVER_ID);
		}
		await expect(main.getByTestId('canonical-problem')).toHaveCount(0);
	});

	test('D-1: a fresh draft re-pinned to another real server lists, submits and tails on THAT server; the pinned scope stays invisible', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		// Fresh draft: explicit pick of Server Beta.
		await main.getByTestId('chat-server-picker').selectOption({ label: 'Server Beta' });
		await expect(main.getByTestId('chat-server-chip')).toContainText('Server Beta');

		// The history list follows the draft choice: SERVER_A's recorded rows
		// never render under the Beta scope.
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();
		await expect(main.getByTestId('history-empty')).toBeVisible();
		const lists = await callsOf(page, 'listCanonicalConversations');
		expect(lists.length).toBeGreaterThanOrEqual(1);
		expect(lists[lists.length - 1].serverId).toBe(SERVER_B);

		// Submit: the create names Beta; every follow-up read matches it.
		await main.getByTestId('compose-message').fill('Beta-scoped question.');
		await main.getByTestId('compose-profile').fill('fixture-fast');
		await main.getByTestId('chat-submit').click();
		await expect(main.getByTestId('canonical-user-message').last()).toContainText(
			'Beta-scoped question.'
		);
		const creates = await callsOf(page, 'createConversation');
		expect(creates).toHaveLength(1);
		expect((creates[0].payload as Record<string, unknown>).server_id).toBe(SERVER_B);
		const conversationId = await page.evaluate(() => window.__maiChatApi!.lastCanonicalId());
		await page.evaluate((id) => {
			window.__maiChatApi!.appendAssistant(id as string, 'beta answer');
			window.__maiChatApi!.completeRun(id as string);
		}, conversationId);
		await expect(main.getByTestId('canonical-assistant-message').last()).toContainText(
			'beta answer'
		);
		await expect(main.getByTestId('canonical-problem')).toHaveCount(0);
		for (const call of await callsOf(page, 'listConversationEvents')) {
			expect(call.serverId).toBe(SERVER_B);
		}
		for (const call of await callsOf(page, 'listConversationMessages')) {
			expect(call.serverId).toBe(SERVER_B);
		}

		// The Beta conversation lands in the (Beta-scoped) drawer history.
		await expect(main.getByTestId('history-row')).toHaveCount(1);
		await expect(main.getByTestId('history-row').first()).toContainText(
			String(conversationId).slice(0, 8)
		);
	});

	test('D-1: a slow history response from a superseded scope never overwrites the current scope page', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');
		await page.evaluate(
			([sentinel, conversationId]) =>
				window.__maiChatApi!.seedCanonicalRow(sentinel, conversationId, 'Late consult'),
			[GLOBAL_CONSULT_SERVER_ID, GLOBAL_CONV]
		);

		// Arm a slow Global list, then unpin and refresh: the Global page is
		// in flight for 600 ms.
		await page.evaluate(() => window.__maiChatApi!.armSlowList(600));
		await main.getByTestId('chat-server-chip-remove').click();
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();

		// While it is in flight, the draft re-pins to Server Beta and
		// refreshes: Beta resolves immediately (empty page).
		await main.getByTestId('chat-server-picker').selectOption({ label: 'Server Beta' });
		await main.getByTestId('history-refresh').click();
		await expect(main.getByTestId('history-empty')).toBeVisible();

		// The late GLOBAL response lands — and must be discarded: the drawer
		// still shows Beta's page, never a cross-server row.
		await page.waitForTimeout(900);
		await expect(main.getByTestId('history-empty')).toBeVisible();
		await expect(main.getByTestId('history-row')).toHaveCount(0);

		// The guard wedged nothing: a fresh Global load renders the row.
		await main.getByTestId('chat-server-chip-remove').click();
		await main.getByTestId('history-refresh').click();
		await expect(main.getByTestId('history-row')).toHaveCount(1);
		await expect(main.getByTestId('history-row').first()).toContainText('Late consult');
	});
});

// ---------------------------------------------------------------------------
// 24b D-2 — the §8 task blocks mount through the panel's services bag. The
// composed defect was BINDING: the dock's bag carried no task methods, so
// TaskPlan stayed unbound and TaskUnderstanding never mounted. These focused
// proofs pin the panel-side contract around the composed coverage in
// desktop-shell.spec.ts (the bag here is the same slice the composed desktop
// binds from the real client).
// ---------------------------------------------------------------------------

test.describe('dock task services mount (24b D-2)', () => {
	test('a restored Global conversation mounts Understanding + Plan on the recorded sentinel scope with the honest evidence-empty state', async ({
		page
	}) => {
		await openFixture(page, 'canonical', { tasks: true });
		const main = page.getByTestId('host-main');
		await page.evaluate(
			([sentinel, conversationId]) =>
				window.__maiChatApi!.seedCanonicalRow(sentinel, conversationId, 'Recorded consult'),
			[GLOBAL_CONSULT_SERVER_ID, GLOBAL_CONV]
		);

		// Unpin → the Global row appears → restore it.
		await main.getByTestId('chat-server-chip-remove').click();
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();
		await expect(main.getByTestId('chat-history-item')).toHaveCount(1);
		await main.getByTestId('chat-history-item').first().click();

		// cti-01 drawer seam: the §8 blocks mount inside the Task Inspector
		// slide-over, NEVER inline in the transcript scroll. The drawer
		// defaults closed and the trigger reads the loaded episode honestly
		// (badge 1/2: steps_executed 1 of steps_total 2).
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-understanding')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-plan')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-outcome')
		).toHaveCount(0);
		await expect(main.getByTestId('chat-tasks-trigger')).toBeEnabled();
		await expect(main.getByTestId('chat-tasks-trigger')).toHaveAttribute('data-state', 'ready');
		await expect(main.getByTestId('chat-tasks-badge')).toHaveText('1/2');
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();

		// Opening the drawer reveals both ready §8 blocks; the transcript
		// scroll keeps zero task children while they are shown.
		await main.getByTestId('chat-tasks-trigger').click();
		await expect(main.getByTestId('chat-tasks-panel')).toBeVisible();
		await expect(main.getByTestId('task-understanding')).toBeVisible();
		await expect(main.getByTestId('task-plan')).toBeVisible();

		// Understanding: ready, with the REAL server-backed sections.
		await expect(main.getByTestId('task-understanding')).toHaveAttribute('data-state', 'ready');
		await expect(main.getByTestId('task-understanding-scope-title')).toHaveText(
			'Fixture plan-only task'
		);
		await expect(main.getByTestId('task-understanding-scope-summary')).toContainText(
			'Understood:'
		);
		await expect(main.getByTestId('task-understanding-scope-meta')).toContainText('evidence 0');
		await expect(main.getByTestId('task-understanding-constraint-row')).toHaveCount(1);
		await expect(main.getByTestId('task-understanding-step-row')).toHaveCount(2);

		// Plan: ready, with real steps; evidence total 0 renders the honest
		// empty state; NO outcome document exists for the plan-only episode,
		// so TaskEvidence stays absent — nothing is fabricated.
		await expect(main.getByTestId('task-plan')).toHaveAttribute('data-state', 'ready');
		await expect(main.getByTestId('task-plan-title')).toHaveText('Fixture plan-only task');
		await expect(main.getByTestId('task-plan-step-row')).toHaveCount(2);
		await expect(main.getByTestId('task-plan-evidence-empty')).toHaveText(
			'No evidence records linked.'
		);
		await expect(main.getByTestId('task-plan-revision-row')).toHaveCount(1);
		await expect(main.getByTestId('task-outcome')).toHaveCount(0);
		await expect(main.getByTestId('canonical-problem')).toHaveCount(0);

		// Mutual exclusion: one drawer at a time. Both drawers overlay the
		// same right edge, so while one is open the sibling trigger sits
		// under the overlay — the switch rides the keyboard path (focus +
		// Enter, exactly what a keyboard user does), never a pointer click
		// through the overlay.
		await main.getByTestId('chat-details-trigger').focus();
		await page.keyboard.press('Enter');
		await expect(main.getByTestId('chat-details-panel')).toBeVisible();
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await main.getByTestId('chat-tasks-trigger').focus();
		await page.keyboard.press('Enter');
		await expect(main.getByTestId('chat-tasks-panel')).toBeVisible();
		await expect(main.getByTestId('chat-details-panel')).toBeHidden();

		// Closing restores the clean transcript view; focus returns to the
		// trigger for both close affordances (✕ and Escape).
		await main.getByTestId('chat-tasks-close').click();
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await expect(main.getByTestId('chat-tasks-trigger')).toBeFocused();
		await main.getByTestId('chat-tasks-trigger').click();
		await expect(main.getByTestId('chat-tasks-panel')).toBeVisible();
		await page.keyboard.press('Escape');
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await expect(main.getByTestId('chat-tasks-trigger')).toBeFocused();

		// Scope: every §8 read rode the row's RECORDED sentinel — never the
		// previously pinned panel scope (the 24a scope rule extends to tasks).
		for (const op of [
			'getConversationTask',
			'listTaskSteps',
			'listTaskConstraints',
			'listTaskEvidence',
			'listTaskRevisions'
		]) {
			const taskCalls = await callsOf(page, op);
			expect(taskCalls.length, op).toBeGreaterThanOrEqual(1);
			for (const call of taskCalls) {
				expect(call.serverId, `${op} scope`).toBe(GLOBAL_CONSULT_SERVER_ID);
			}
		}
	});

	test('a server-scoped conversation mounts the task blocks on ITS recorded server and refuses a foreign scope', async ({
		page
	}) => {
		await openFixture(page, 'canonical', { tasks: true });
		const main = page.getByTestId('host-main');

		// Default draft: pinned Server Alpha. The preseeded row is recorded
		// on Alpha; restore it from the history selector.
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();
		await main.getByTestId('chat-history-item').filter({ hasText: 'Restore me' }).click();

		await expect(main.getByTestId('task-understanding')).toHaveAttribute('data-state', 'ready');
		await expect(main.getByTestId('task-plan')).toHaveAttribute('data-state', 'ready');

		// cti-01 drawer seam: the blocks mount inside the Task Inspector
		// drawer on the recorded scope — the transcript scroll stays clean,
		// the trigger reads the loaded episode, and the drawer opens with
		// both blocks visible.
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-understanding')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-plan')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-outcome')
		).toHaveCount(0);
		await expect(main.getByTestId('chat-tasks-trigger')).toBeEnabled();
		await expect(main.getByTestId('chat-tasks-badge')).toHaveText('1/2');
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await main.getByTestId('chat-tasks-trigger').click();
		await expect(main.getByTestId('chat-tasks-panel')).toBeVisible();
		await expect(main.getByTestId('task-understanding')).toBeVisible();
		await expect(main.getByTestId('task-plan')).toBeVisible();
		await main.getByTestId('chat-tasks-close').click();
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await expect(main.getByTestId('chat-tasks-trigger')).toBeFocused();
		for (const op of [
			'getConversationTask',
			'listTaskSteps',
			'listTaskConstraints',
			'listTaskEvidence',
			'listTaskRevisions'
		]) {
			const taskCalls = await callsOf(page, op);
			expect(taskCalls.length, op).toBeGreaterThanOrEqual(1);
			for (const call of taskCalls) {
				expect(call.serverId, `${op} scope`).toBe(SERVER_A);
			}
		}

		// The scope-blind rule (C8): the SAME conversation probed under
		// Server Beta's scope 404s — no existence or content leak.
		const probe = await page.evaluate(() => window.__maiChatApi!.probeCrossServerTask());
		expect(probe.code).toBe('task_not_found');
	});

	test('without a bound task slice both blocks fail closed honestly and make zero task requests', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();
		await main.getByTestId('chat-history-item').filter({ hasText: 'Restore me' }).click();
		await expect(main.getByTestId('canonical-user-message').first()).toContainText(
			'Restore question'
		);

		// The bag carries the canonical slice but NO task methods: both §8
		// blocks render their explicit unbound state — nothing fetched,
		// nothing fabricated, no task collections in the DOM. (cti-01: the
		// blocks live in the CLOSED Task Inspector drawer — their honest
		// notes stay mounted behind display:none, so presence, not
		// visibility, is the assertion here.)
		await expect(main.getByTestId('task-understanding')).toHaveAttribute(
			'data-state',
			'unbound'
		);
		await expect(main.getByTestId('task-understanding-unbound')).toHaveCount(1);
		await expect(main.getByTestId('task-plan')).toHaveAttribute('data-state', 'unbound');
		await expect(main.getByTestId('task-plan-unbound')).toHaveCount(1);
		await expect(main.getByTestId('task-understanding-scope-title')).toHaveCount(0);
		await expect(main.getByTestId('task-plan-title')).toHaveCount(0);

		// cti-01 drawer seam: with no bound slice the inspector cannot open
		// — a disabled 'unbound' trigger, no badge, a hidden drawer, and a
		// transcript scroll that keeps zero task children at real height.
		await expect(main.getByTestId('chat-tasks-trigger')).toBeDisabled();
		await expect(main.getByTestId('chat-tasks-trigger')).toHaveAttribute(
			'data-state',
			'unbound'
		);
		await expect(main.getByTestId('chat-tasks-badge')).toHaveCount(0);
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-understanding')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-plan')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-outcome')
		).toHaveCount(0);
		const unboundDrawerTranscriptHeight = await main
			.locator('.mai-chat__conversation-scroll')
			.evaluate((el: HTMLElement) => el.clientHeight);
		expect(unboundDrawerTranscriptHeight, 'unbound transcript height').toBeGreaterThan(0);
		for (const op of [
			'getConversationTask',
			'listTaskSteps',
			'listTaskConstraints',
			'listTaskEvidence',
			'listTaskRevisions',
			'submitTaskCorrection'
		]) {
			expect(await callsOf(page, op), op).toHaveLength(0);
		}
	});
});

// ---------------------------------------------------------------------------
// 24c D-3 — terminal canonical reopen VISIBILITY. V6-retest proved the live
// defect: a completed canonical conversation (active_run_id NULL) restores
// with the legacy fold null (`display = refreshed ?? conversation` stays
// null), the section got `mai-chat__section--idle`, and the docked rule
// `.mai-chat--docked .mai-chat__section--idle { display:none }` hid the
// ENTIRE conversation — transcript, Understanding and Plan. These focused
// cases drive the EXISTING canonical restore seam (history rows) at 360×640
// and pin the state condition directly: an open canonical identity must
// never carry the idle class. The DOCKED visibility proof (computed
// display:none red-before/green-after) lives in the composed DesktopShell
// suite, which mounts the real bottom dock. The blank-draft idle behavior
// must stay exactly as it is.
// ---------------------------------------------------------------------------

test.describe('terminal canonical reopen visibility (24c D-3)', () => {
	test.use({ viewport: { width: 360, height: 640 } });

	test('a completed canonical conversation (active_run_id null) reopens with transcript, Understanding and Plan visible', async ({
		page
	}) => {
		await openFixture(page, 'canonical', { tasks: true });
		// Pocket-viewport convention (chat layout tests): isolate the main
		// host so sibling sections cannot intercept the compose interaction.
		await page.addStyleTag({
			content: 'body { margin: 0; } #app > section:not([data-testid="host-main"]) { display:none; }'
		});
		const main = page.getByTestId('host-main');

		// Finish a conversation: submit, the producer appends the reply and
		// releases the writer lease — server truth: active_run_id NULL after.
		// (Canonical submits ride createConversation — never submitMessage.)
		await main.getByTestId('compose-message').fill('Terminal question about the plan');
		await main.getByTestId('compose-profile').fill('fixture-fast');
		await main.getByTestId('chat-submit').click();
		await expect(
			main
				.getByTestId('canonical-user-message')
				.filter({ hasText: 'Terminal question about the plan' })
		).toBeVisible();
		const conversationId = await page.evaluate(() => window.__maiChatApi!.lastCanonicalId());
		expect(conversationId).not.toBeNull();
		await page.evaluate((id) => {
			window.__maiChatApi!.appendAssistant(id as string, 'Terminal recorded answer');
			window.__maiChatApi!.completeRun(id as string);
		}, conversationId);
		await expect(main.getByTestId('conversation-state')).toHaveText('Succeeded');

		// New Chat: the intentional blank-draft idle state stays untouched —
		// the repair must never fake an open conversation here.
		await main.getByTestId('chat-new-session').click();
		await expect(main.getByTestId('chat-conversation')).toHaveClass(/mai-chat__section--idle/);
		await expect(main.getByTestId('chat-none')).toBeVisible();
		await expect(main.getByTestId('task-understanding')).toHaveCount(0);

		// Reopen the COMPLETED conversation from drawer-style history rows.
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();
		await main
			.getByTestId('chat-history-item')
			.filter({ hasText: String(conversationId).slice(0, 8) })
			.click();

		// An open canonical identity must NOT be idle: real transcript, real
		// §8 data, nonzero box — not a hidden section, not a fabricated run.
		await expect(main.getByTestId('chat-conversation')).toBeVisible();
		await expect(main.getByTestId('chat-conversation')).not.toHaveClass(
			/mai-chat__section--idle/
		);
		const box = await main.getByTestId('chat-conversation').boundingBox();
		expect(box, 'completed reopen conversation box').not.toBeNull();
		expect(box!.height).toBeGreaterThan(0);
		await expect(main.getByTestId('canonical-user-message')).toContainText(
			'Terminal question about the plan'
		);
		await expect(main.getByTestId('canonical-assistant-message')).toContainText(
			'Terminal recorded answer'
		);
		await expect(main.getByTestId('task-understanding')).toHaveAttribute('data-state', 'ready');
		await expect(main.getByTestId('task-understanding-scope-title')).toHaveText(
			'Fixture plan-only task'
		);
		await expect(main.getByTestId('task-understanding-constraint-row')).toHaveCount(1);
		await expect(main.getByTestId('task-understanding-step-row')).toHaveCount(2);
		await expect(main.getByTestId('task-plan')).toHaveAttribute('data-state', 'ready');

		// cti-01 drawer seam: the §8 blocks live in the Task Inspector
		// drawer — the transcript scroll keeps zero task children, the
		// trigger reads the restored episode, and the drawer opens with
		// the real blocks inside (the 24d geometry proof below runs
		// against this OPEN drawer; the closed snapshot would measure
		// display:none zeros by design).
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-understanding')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-plan')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-outcome')
		).toHaveCount(0);
		await expect(main.getByTestId('chat-tasks-trigger')).toBeEnabled();
		await expect(main.getByTestId('chat-tasks-trigger')).toHaveAttribute('data-state', 'ready');
		await expect(main.getByTestId('chat-tasks-badge')).toHaveText('1/2');
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await main.getByTestId('chat-tasks-trigger').click();
		await expect(main.getByTestId('chat-tasks-panel')).toBeVisible();
		await expect(main.getByTestId('task-understanding')).toBeVisible();
		await expect(main.getByTestId('task-plan')).toBeVisible();
		await expect(main.getByTestId('task-plan-evidence-empty')).toBeVisible();
		await expect(main.getByTestId('canonical-problem')).toHaveCount(0);

		// 24d grid usability: the focused terminal reopen must be genuinely
		// usable at 360×640 — full-width task blocks with readable text
		// columns, a transcript with real height, and no horizontal overflow.
		const geometry24d = await main
			.getByTestId('chat-conversation')
			.evaluate((section: HTMLElement) => {
				const clientWidth = (selector: string) =>
					section.querySelector<HTMLElement>(selector)?.clientWidth ?? null;
				const scroll = section.querySelector<HTMLElement>('.mai-chat__conversation-scroll');
				return {
					sectionWidth: Math.round(section.getBoundingClientRect().width),
					sectionClientWidth: section.clientWidth,
					sectionScrollWidth: section.scrollWidth,
					understandingWidth: clientWidth('[data-testid="task-understanding"]'),
					planWidth: clientWidth('[data-testid="task-plan"]'),
					scopeTitleWidth: clientWidth('[data-testid="task-understanding-scope-title"]'),
					scopeSummaryWidth: clientWidth('[data-testid="task-understanding-scope-summary"]'),
					constraintRowWidth: clientWidth('[data-testid="task-understanding-constraint-row"]'),
					planTitleWidth: clientWidth('[data-testid="task-plan-title"]'),
					planEvidenceEmptyWidth: clientWidth('[data-testid="task-plan-evidence-empty"]'),
					transcriptClientHeight: scroll?.clientHeight ?? 0,
					docScrollWidth: document.documentElement.scrollWidth,
					viewportWidth: window.innerWidth
				};
			});
		console.log('[24D-PROBE] focused terminal geometry', JSON.stringify(geometry24d));
		expect(geometry24d.sectionWidth, 'focused terminal section width').toBeGreaterThanOrEqual(200);
		expect(geometry24d.understandingWidth, 'focused terminal understanding width').toBeGreaterThanOrEqual(200);
		expect(geometry24d.planWidth, 'focused terminal plan width').toBeGreaterThanOrEqual(200);
		for (const [name, width] of [
			['scope title', geometry24d.scopeTitleWidth],
			['scope summary', geometry24d.scopeSummaryWidth],
			['constraint row', geometry24d.constraintRowWidth],
			['plan title', geometry24d.planTitleWidth],
			['plan evidence empty', geometry24d.planEvidenceEmptyWidth]
		] as const) {
			expect(width, `focused terminal ${name} text width`).toBeGreaterThan(0);
		}
		expect(geometry24d.transcriptClientHeight, 'focused terminal transcript height').toBeGreaterThan(0);
		expect(
			geometry24d.sectionScrollWidth,
			'focused terminal section horizontal overflow'
		).toBeLessThanOrEqual(geometry24d.sectionClientWidth + 1);
		expect(geometry24d.docScrollWidth, 'focused terminal page horizontal overflow').toBeLessThanOrEqual(
			geometry24d.viewportWidth + 1
		);

		// Closing the drawer restores the clean transcript view; focus
		// returns to the trigger for both close affordances (✕ and Escape).
		await main.getByTestId('chat-tasks-close').click();
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await expect(main.getByTestId('chat-tasks-trigger')).toBeFocused();
		await main.getByTestId('chat-tasks-trigger').click();
		await expect(main.getByTestId('chat-tasks-panel')).toBeVisible();
		await page.keyboard.press('Escape');
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await expect(main.getByTestId('chat-tasks-trigger')).toBeFocused();
	});

	test('a plain canonical conversation without a task reopens with visible messages and no task fabrication', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		// The recorded Global consult (the V6 9ce236c1 shape: plain, no task).
		await page.evaluate(
			([sentinel, rowId]) =>
				window.__maiChatApi!.seedCanonicalRow(sentinel, rowId, 'Plain recorded consult'),
			[GLOBAL_CONSULT_SERVER_ID, GLOBAL_CONV]
		);
		await main.getByTestId('chat-server-chip-remove').click();
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('history-refresh').click();
		await expect(main.getByTestId('chat-history-item')).toHaveCount(1);
		await main.getByTestId('chat-history-item').first().click();

		await expect(main.getByTestId('chat-conversation')).toBeVisible();
		await expect(main.getByTestId('chat-conversation')).not.toHaveClass(
			/mai-chat__section--idle/
		);
		const box = await main.getByTestId('chat-conversation').boundingBox();
		expect(box, 'plain reopen conversation box').not.toBeNull();
		expect(box!.height).toBeGreaterThan(0);
		await expect(main.getByTestId('canonical-user-message')).toContainText(
			'Seeded question — Plain recorded consult'
		);
		await expect(main.getByTestId('canonical-assistant-message')).toContainText(
			'Seeded recorded answer — Plain recorded consult'
		);
		// No task slice bound: both §8 blocks fail closed honestly and zero
		// task requests leave the page — absence, never fabrication. (The
		// unbound note's own visibility at this viewport belongs to the 24b
		// suite; here the §8 state honesty rides the section data-state and
		// its recorded note text.)
		await expect(main.getByTestId('task-understanding')).toHaveAttribute('data-state', 'unbound');
		await expect(main.getByTestId('task-understanding')).toContainText(
			'No task services are bound; nothing is fetched or fabricated.'
		);
		await expect(main.getByTestId('task-plan')).toHaveAttribute('data-state', 'unbound');
		await expect(main.getByTestId('task-plan')).toContainText(
			'No task services are bound; nothing is fetched or fabricated.'
		);
		for (const op of [
			'getConversationTask',
			'listTaskSteps',
			'listTaskConstraints',
			'listTaskEvidence',
			'listTaskRevisions'
		]) {
			expect(await callsOf(page, op), op).toHaveLength(0);
		}
		// cti-01 drawer seam: with no bound slice the inspector cannot open
		// — a disabled 'unbound' trigger, no badge, a hidden drawer, and a
		// transcript scroll that keeps zero task children. The unbound
		// notes stay mounted INSIDE the closed drawer (presence above);
		// their readable geometry belongs to the drawer's own open state,
		// which is unreachable exactly because the honesty gate holds.
		await expect(main.getByTestId('chat-tasks-trigger')).toBeDisabled();
		await expect(main.getByTestId('chat-tasks-trigger')).toHaveAttribute(
			'data-state',
			'unbound'
		);
		await expect(main.getByTestId('chat-tasks-badge')).toHaveCount(0);
		await expect(main.getByTestId('chat-tasks-panel')).toBeHidden();
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-understanding')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-plan')
		).toHaveCount(0);
		await expect(
			main.locator('.mai-chat__conversation-scroll').getByTestId('task-outcome')
		).toHaveCount(0);
		// 24d grid usability: the transcript keeps real height at this
		// viewport — not the 0-height row R-24c measured.
		const unbound24d = await main
			.getByTestId('chat-conversation')
			.evaluate((section: HTMLElement) => {
				const scroll = section.querySelector<HTMLElement>('.mai-chat__conversation-scroll');
				return {
					sectionWidth: Math.round(section.getBoundingClientRect().width),
					transcriptClientHeight: scroll?.clientHeight ?? 0
				};
			});
		console.log('[24D-PROBE] focused unbound geometry', JSON.stringify(unbound24d));
		expect(unbound24d.sectionWidth, 'focused unbound section width').toBeGreaterThanOrEqual(200);
		expect(unbound24d.transcriptClientHeight, 'focused unbound transcript height').toBeGreaterThan(0);
		await expect(main.getByTestId('canonical-problem')).toHaveCount(0);
	});

	test('a fail-closed restore 404 renders its error visibly instead of collapsing silently', async ({
		page
	}) => {
		await openFixture(page, 'canonical');
		const main = page.getByTestId('host-main');

		// Arm the producer failure the restore must surface: the detail read
		// answers with the scope-blind 404 (C8 preserved, C05 honesty kept).
		await page.evaluate(() =>
			window.__maiChatApi!.failNextRead({
				status: 404,
				code: 'not_found',
				message: 'no conversation in the requesting server scope'
			})
		);
		await main.getByTestId('chat-history-toggle').click();
		await main.getByTestId('chat-history-item').filter({ hasText: 'Restore me' }).click();

		// The structured error must be VISIBLE: pre-repair the restore catch
		// wiped the problem state and collapsed to the blank draft — a silent
		// failure. With the open canonical identity the alert stays mounted.
		await expect(main.getByTestId('canonical-problem')).toBeVisible();
		await expect(main.getByTestId('canonical-problem')).toHaveAttribute(
			'data-error-code',
			'not_found'
		);
		await expect(main.getByTestId('canonical-problem')).toContainText(
			'no conversation in the requesting server scope'
		);
		await expect(main.getByTestId('chat-conversation')).toBeVisible();
		await expect(main.getByTestId('chat-conversation')).not.toHaveClass(
			/mai-chat__section--idle/
		);

		// 24d: the 404 error surface must stay READABLE — real width and a
		// horizontal text column, never a crushed sliver or a vertical badge.
		const problem24d = await main.getByTestId('canonical-problem').evaluate((el: HTMLElement) => {
			const r = el.getBoundingClientRect();
			return {
				width: Math.round(r.width),
				height: Math.round(r.height),
				textWidth: el.clientWidth
			};
		});
		console.log('[24D-PROBE] focused 404 geometry', JSON.stringify(problem24d));
		expect(problem24d.width, '404 problem width').toBeGreaterThanOrEqual(200);
		expect(problem24d.textWidth, '404 problem text width').toBeGreaterThan(100);
		expect(problem24d.height, '404 problem height').toBeLessThanOrEqual(200);
	});
});
