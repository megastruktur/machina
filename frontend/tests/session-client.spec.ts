import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { ApiError, createMaiApiClient } from '../src/lib/api/client';
import {
	createSessionChannelController,
	sessionChannelUrl,
	type ChannelSocket
} from '../src/lib/chat/session_client';

/** Node globals (ambient: the project tsconfig targets browser-first code). */
declare const process: { env: Record<string, string | undefined> };

/**
 * session-client.spec.ts — D07 proofs for the session-first client surface
 * (frontend/src/lib/api/client.ts agent-sessions methods +
 * frontend/src/lib/chat/session_client.ts channel controller +
 * ChatDrawerSection session-first wiring).
 *
 * Three layers, one file (the sibling-spec convention):
 *
 * 1. REST boundary (Node, injected fetch): the typed
 *    `/api/v1/agent-sessions/**` methods carry the CSRF double submit on
 *    mutations only, build honest bounded query strings (`server_id`,
 *    `status`, `tag`, `before_seq`, `after_seq`, `limit`, `offset`), and
 *    surface C01 envelopes as typed `ApiError`s.
 * 2. Channel controller (Node, injectable socket factory): the `hello`
 *    handshake with the committed cursor, snapshot/entries/committed merge
 *    deduplicated by seq with `highestCommittedSeq` advancing ONLY on
 *    committed delivery, volatile delta drafts deduplicated by `delta_id`
 *    and reconciled away by the run's committed entry, `resync_required`
 *    answering with `subscribe` + `ack` from the cursor, bounded one-shot
 *    reconnect resuming strictly from the cursor, and clean teardown with
 *    zero polling timers.
 * 3. Drawer composition (real Chromium, in-memory Vite production build of
 *    a virtual entry importing the REAL client + the REAL
 *    ChatDrawerSection): session rename over `session_id`, HistoryTags
 *    pagination over the agent-session tag route with the conversation/run
 *    fallbacks, identity-routed per row (Documents excerpt attach into a
 *    session is proven by docs.spec.ts's attach scenario, which D07 rewired
 *    to `attachToSession`).
 */

// ---------------------------------------------------------------------------
// Node layer: injected-fetch REST boundary
// ---------------------------------------------------------------------------

interface CapturedRequest {
	url: string;
	method: string;
	headers: Record<string, string>;
	body: string | null;
}

interface FetchRecorder {
	fetch: typeof fetch;
	calls: CapturedRequest[];
}

/** Origin the stub resolves relative client paths against (Node has no base). */
const STUB_ORIGIN = 'http://mai.test';

function stubFetch(handler: (request: CapturedRequest) => Response): FetchRecorder {
	const calls: CapturedRequest[] = [];
	const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
		// Node's Request rejects relative URLs; resolve against a stub origin.
		const resolved =
			input instanceof Request ? input : new Request(new URL(input.toString(), STUB_ORIGIN), init);
		const captured: CapturedRequest = {
			url: resolved.url,
			method: resolved.method,
			headers: Object.fromEntries(resolved.headers.entries()),
			body: resolved.method === 'GET' ? null : await resolved.clone().text()
		};
		const response = await handler(captured);
		calls.push(captured);
		return response;
	}) as typeof fetch;
	return { fetch: impl, calls };
}

/** One-shot test delay (Promise.withResolvers; no polling loop). */
function delay(ms: number): Promise<void> {
	const { promise, resolve } = Promise.withResolvers<void>();
	setTimeout(resolve, ms);
	return promise;
}

function jsonResponse(status: number, body: unknown): Response {
	return new Response(body === undefined ? null : JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

const SESSION_A = '11111111-1111-4111-8111-111111111111';
const SERVER_A = '5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60';

const SESSION_VIEW = {
	session_id: SESSION_A,
	server_id: SERVER_A,
	source: 'interactive',
	status: 'idle',
	title: null,
	created_at: '2026-01-01T00:00:00Z',
	updated_at: '2026-01-01T00:00:00Z',
	latest_seq: 0,
	entry_count: 0,
	oldest_seq: null,
	latest_byte_size: 0
};

const ENVELOPE = {
	error: {
		code: 'session_not_found',
		message: 'no session found for this identity',
		request_id: 'req-d07',
		fields: {}
	}
} as const;

test.describe('REST boundary (injected fetch)', () => {
	test('mutations carry the CSRF double submit; GETs do not', async () => {
		const { fetch, calls } = stubFetch((request) => {
			if (request.method === 'GET') return jsonResponse(200, { items: [], limit: 20, offset: 0, total: 0, has_more: false });
			if (request.method === 'PATCH') return jsonResponse(200, SESSION_VIEW);
			return jsonResponse(200, SESSION_VIEW);
		});
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 'csrf-d07' });

		await client.createAgentSession({
			server_id: SERVER_A,
			source: 'interactive',
			client_request_id: '44444444-4444-4444-8444-444444444444'
		});
		await client.listAgentSessions({ server_id: SERVER_A });
		await client.renameAgentSession(SESSION_A, { title: 'renamed' });

		const create = calls[0];
		expect(create.method).toBe('POST');
		expect(create.url).toContain('/api/v1/agent-sessions');
		expect(create.headers['x-csrf-token']).toBe('csrf-d07');
		const listed = calls[1];
		expect(listed.method).toBe('GET');
		expect(listed.headers['x-csrf-token']).toBeUndefined();
		const rename = calls[2];
		expect(rename.method).toBe('PATCH');
		expect(rename.url).toContain(`/api/v1/agent-sessions/${SESSION_A}`);
		expect(rename.headers['x-csrf-token']).toBe('csrf-d07');
		expect(JSON.parse(rename.body ?? '')).toEqual({ title: 'renamed' });
	});

	test('list/entries queries carry their explicit scope and bounded cursors', async () => {
		const { fetch, calls } = stubFetch(() =>
			jsonResponse(200, { items: [], limit: 20, offset: 0, total: 0, has_more: false })
		);
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 'csrf-d07' });

		await client.listAgentSessions({
			server_id: SERVER_A,
			status: 'idle',
			tag: '99999999-9999-4999-8999-999999999999',
			limit: 10,
			offset: 20
		});
		await client.listAgentSessionEntries(SESSION_A, {
			server_id: SERVER_A,
			before_seq: 100,
			after_seq: 40,
			limit: 50
		});
		await client.readAgentSession(SESSION_A, SERVER_A);

		const listUrl = new URL(calls[0].url);
		expect(listUrl.pathname).toBe('/api/v1/agent-sessions');
		expect(listUrl.searchParams.get('server_id')).toBe(SERVER_A);
		expect(listUrl.searchParams.get('status')).toBe('idle');
		expect(listUrl.searchParams.get('limit')).toBe('10');
		expect(listUrl.searchParams.get('offset')).toBe('20');
		const entriesUrl = new URL(calls[1].url);
		expect(entriesUrl.pathname).toBe(`/api/v1/agent-sessions/${SESSION_A}/history`);
		expect(entriesUrl.searchParams.get('server_id')).toBe(SERVER_A);
		expect(entriesUrl.searchParams.get('before_seq')).toBe('100');
		expect(entriesUrl.searchParams.get('after_seq')).toBe('40');
		expect(entriesUrl.searchParams.get('limit')).toBe('50');
		const readUrl = new URL(calls[2].url);
		expect(readUrl.pathname).toBe(`/api/v1/agent-sessions/${SESSION_A}`);
		expect(readUrl.searchParams.get('server_id')).toBe(SERVER_A);
	});

	test('C01 error envelopes surface as typed ApiError with stable code', async () => {
		const { fetch } = stubFetch(() => jsonResponse(404, ENVELOPE));
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 'csrf-d07' });
		const failure = await client.readAgentSession(SESSION_A, SERVER_A).catch((error: unknown) => error);
		expect(failure).toBeInstanceOf(ApiError);
		expect((failure as ApiError).status).toBe(404);
		expect((failure as ApiError).code).toBe('session_not_found');
	});

	test('mutations fail closed client-side without a CSRF token', async () => {
		let networkCalls = 0;
		const { fetch } = stubFetch(() => {
			networkCalls += 1;
			return jsonResponse(200, SESSION_VIEW);
		});
		const client = createMaiApiClient({ fetch, getCsrfToken: () => null });
		await expect(
			client.submitAgentSessionTurn(SESSION_A, {
				session_id: SESSION_A,
				text: 'x',
				client_request_id: '55555555-5555-4555-8555-555555555555'
			})
		).rejects.toThrow(/no mai_csrf token/);
		expect(networkCalls, 'no mutation may leave without a token').toBe(0);
	});
});

// ---------------------------------------------------------------------------
// Node layer: channel controller over an injectable socket factory
// ---------------------------------------------------------------------------

class FakeSocket implements ChannelSocket {
	sent: string[] = [];
	closed = false;
	closeCode: number | null = null;
	onopen: (() => void) | null = null;
	onclose: ((event: { code?: number; reason?: string }) => void) | null = null;
	onerror: ((event: unknown) => void) | null = null;
	onmessage: ((event: { data: unknown }) => void) | null = null;

	send(data: string): void {
		this.sent.push(data);
	}

	close(code?: number, _reason?: string): void {
		this.closed = true;
		this.closeCode = code ?? null;
	}

	// --- server-side simulation helpers (tests only) ------------------------
	open(): void {
		this.onopen?.();
	}

	serverSend(frame: unknown): void {
		this.onmessage?.({ data: JSON.stringify(frame) });
	}

	serverClose(): void {
		this.onclose?.({ code: 1006, reason: 'transport lost' });
	}

	frames(): Array<Record<string, unknown>> {
		return this.sent.map((raw) => JSON.parse(raw) as Record<string, unknown>);
	}
}

function makeEntry(seq: number, runId: string | null = null) {
	return {
		schema_version: 1,
		entry_id: `aaaaaaaa-aaaa-4aaa-8aaa-${String(seq).padStart(12, '0')}`,
		session_id: SESSION_A,
		seq,
		run_id: runId,
		durable_source_id: `seed-${seq}`,
		created_at: '2026-01-01T00:00:00Z',
		payload: { kind: 'user_message', text: `entry ${seq}`, client_request_id: `c-${seq}`, source: 'api' }
	};
}

function makeSockets(): { sockets: FakeSocket[]; socketFactory: (url: string) => ChannelSocket } {
	const sockets: FakeSocket[] = [];
	return {
		sockets,
		socketFactory: (url: string) => {
			const socket = new FakeSocket();
			void url;
			sockets.push(socket);
			return socket;
		}
	};
}

async function openChannel(sockets: FakeSocket[]): Promise<void> {
	// The controller opens the socket synchronously inside connect(); the
	// handshake completes when the fixture "server" answers hello_ok.
	await Promise.resolve();
	expect(sockets.length).toBeGreaterThanOrEqual(1);
	sockets[0].open();
	await delay(0);
	sockets[0].serverSend({ type: 'hello_ok', protocol_version: 1, session_id: SESSION_A, server_id: SERVER_A, stream_id: 's', latest_seq: 0 });
	await delay(0);
}

test.describe('channel controller (injectable socket factory)', () => {
	test('channel URL carries only the session id and scope pin', () => {
		expect(sessionChannelUrl(SESSION_A)).toBe(
			`/api/v1/agent-sessions/${SESSION_A}/channel`
		);
		expect(sessionChannelUrl(SESSION_A, SERVER_A)).toBe(
			`/api/v1/agent-sessions/${SESSION_A}/channel?server_id=${SERVER_A}`
		);
	});

	test('hello carries the protocol, CSRF proof and the committed cursor', async () => {
		const { sockets, socketFactory } = makeSockets();
		const controller = createSessionChannelController({
			sessionId: SESSION_A,
			serverId: SERVER_A,
			getCsrfToken: () => 'csrf-d07',
			socketFactory,
			reconnectDelaysMs: [10, 10]
		});
		controller.connect();
		await openChannel(sockets);

		const hello = sockets[0].frames()[0];
		expect(hello).toEqual({
			type: 'hello',
			protocol_version: 1,
			after_seq: null, // no committed cursor yet
			csrf_token: 'csrf-d07',
			client_request_id: expect.any(String)
		});

		// Commit three entries, then reconnect: the resumed hello replays
		// strictly from the durable cursor.
		sockets[0].serverSend({ type: 'snapshot', session_id: SESSION_A, entries: [makeEntry(1), makeEntry(2)], latest_seq: 2, has_more: false, has_older: false, byte_size: 1, truncated_by_bytes: false });
		await delay(0);
		expect(controller.highestCommittedSeq()).toBe(2);
		sockets[0].serverClose();
		await delay(40);
		expect(sockets.length).toBe(2);
		sockets[1].open();
		await delay(0);
		const resumed = sockets[1].frames()[0];
		expect(resumed['type']).toBe('hello');
		expect(resumed['after_seq']).toBe(2);
		controller.dispose();
	});

	test('snapshot/entries merge dedupes by seq; only committed delivery advances the cursor', async () => {
		const { sockets, socketFactory } = makeSockets();
		const controller = createSessionChannelController({
			sessionId: SESSION_A,
			getCsrfToken: () => 'csrf-d07',
			socketFactory
		});
		controller.connect();
		await openChannel(sockets);

		// Out-of-order snapshot entries arrive merged SORTED.
		sockets[0].serverSend({ type: 'snapshot', session_id: SESSION_A, entries: [makeEntry(5), makeEntry(2)], latest_seq: 5, has_more: true, has_older: false, byte_size: 1, truncated_by_bytes: false });
		await delay(0);
		expect(controller.entries().map((entry) => entry.seq)).toEqual([2, 5]);
		expect(controller.highestCommittedSeq()).toBe(5);

		// A replay page re-serving seq 2 deduplicates in place.
		sockets[0].serverSend({ type: 'entries', entries: [makeEntry(2), makeEntry(6)], latest_seq: 6, has_more: false, has_older: true, next_before_seq: 2 });
		await delay(0);
		expect(controller.entries().map((entry) => entry.seq)).toEqual([2, 5, 6]);
		expect(controller.highestCommittedSeq()).toBe(6);

		// A volatile delta NEVER advances the cursor and NEVER enters the log.
		sockets[0].serverSend({ type: 'delta', stream_id: 'stream-1', delta_id: 'd1', text: 'volatile', run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr' });
		await delay(0);
		expect(controller.highestCommittedSeq()).toBe(6);
		expect(controller.entries().map((entry) => entry.seq)).toEqual([2, 5, 6]);
		controller.dispose();
	});

	test('duplicate/out-of-order deltas dedupe into one draft; the committed entry reconciles it', async () => {
		const { sockets, socketFactory } = makeSockets();
		const runId = 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr';
		let lastDraft: SessionStreamDraftLike | null = null;
		const controller = createSessionChannelController({
			sessionId: SESSION_A,
			getCsrfToken: () => 'csrf-d07',
			socketFactory,
			handlers: {
				onStreamDraftChanged: (draft) => {
					lastDraft = draft;
				}
			}
		});
		controller.connect();
		await openChannel(sockets);

		sockets[0].serverSend({ type: 'delta', stream_id: 'stream-1', delta_id: 'd1', text: 'Hel', run_id: runId });
		sockets[0].serverSend({ type: 'delta', stream_id: 'stream-1', delta_id: 'd1', text: 'Hel', run_id: runId }); // duplicate id
		sockets[0].serverSend({ type: 'delta', stream_id: 'stream-1', delta_id: 'd2', text: 'lo', run_id: runId }); // out of order arrival
		await delay(0);
		const draft = controller.streamDraft('stream-1');
		expect(draft?.text).toBe('Hello'); // duplicate dropped, arrival order kept
		expect(controller.highestCommittedSeq()).toBeNull(); // deltas never commit

		// The run's committed entry replaces the volatile draft (no phantom
		// assistant message beside the durable truth).
		sockets[0].serverSend({ type: 'committed', stream_id: 'stream-1', entry: makeEntry(1, runId) });
		await delay(0);
		expect(controller.streamDraft('stream-1')).toBeNull();
		expect(lastDraft).toBeNull();
		expect(controller.entries().map((entry) => entry.seq)).toEqual([1]);
		expect(controller.highestCommittedSeq()).toBe(1);
		controller.dispose();
	});

	test('resync_required discards volatile deltas and answers subscribe+ack from the cursor', async () => {
		const { sockets, socketFactory } = makeSockets();
		let resynced = false;
		const controller = createSessionChannelController({
			sessionId: SESSION_A,
			getCsrfToken: () => 'csrf-d07',
			socketFactory,
			handlers: {
				onResyncRequired: () => {
					resynced = true;
				}
			}
		});
		controller.connect();
		await openChannel(sockets);

		sockets[0].serverSend({ type: 'snapshot', session_id: SESSION_A, entries: [makeEntry(1), makeEntry(4)], latest_seq: 4, has_more: false, has_older: false, byte_size: 1, truncated_by_bytes: false });
		sockets[0].serverSend({ type: 'delta', stream_id: 'stream-1', delta_id: 'd1', text: 'volatile', run_id: null });
		await delay(0);
		expect(controller.streamDrafts().length).toBe(1);

		sockets[0].serverSend({ type: 'resync_required', reason_code: 'gap_detected', latest_seq: 9 });
		await delay(0);
		expect(resynced).toBe(true);
		expect(controller.connectionState()).toBe('resync');
		// Volatile drafts are gone; the durable log stays untouched.
		expect(controller.streamDrafts()).toEqual([]);
		expect(controller.entries().map((entry) => entry.seq)).toEqual([1, 4]);
		const frames = sockets[0].frames();
		const subscribe = frames.find((frame) => frame['type'] === 'subscribe');
		const ack = frames.find((frame) => frame['type'] === 'ack');
		expect(subscribe).toEqual({
			type: 'subscribe',
			after_seq: 4,
			client_request_id: expect.any(String)
		});
		expect(ack).toEqual({ type: 'ack', after_seq: 4 });
		controller.dispose();
	});

	test('reconnect resumes strictly from the cursor and stays bounded', async () => {
		const { sockets, socketFactory } = makeSockets();
		const controller = createSessionChannelController({
			sessionId: SESSION_A,
			getCsrfToken: () => 'csrf-d07',
			socketFactory,
			reconnectDelaysMs: [10, 10, 10]
		});
		controller.connect();
		await openChannel(sockets);
		sockets[0].serverSend({ type: 'snapshot', session_id: SESSION_A, entries: [makeEntry(7)], latest_seq: 7, has_more: false, has_older: false, byte_size: 1, truncated_by_bytes: false });
		await delay(0);
		expect(controller.connectionState()).toBe('open');

		sockets[0].serverClose();
		await delay(60);
		expect(controller.connectionState()).toBe('reconnecting');
		expect(sockets.length).toBe(2);
		sockets[1].open();
		await delay(0);
		const resumed = sockets[1].frames()[0];
		expect(resumed['after_seq']).toBe(7);
		controller.dispose();
	});

	test('close() and dispose() stop the reconnect ladder with zero polling timers', async () => {
		const { sockets, socketFactory } = makeSockets();
		const controller = createSessionChannelController({
			sessionId: SESSION_A,
			getCsrfToken: () => 'csrf-d07',
			socketFactory,
			reconnectDelaysMs: [10]
		});
		controller.connect();
		await openChannel(sockets);
		controller.close();
		expect(controller.connectionState()).toBe('closed');
		sockets[0].serverClose(); // late transport close AFTER user close
		await delay(60);
		expect(sockets.length).toBe(1); // no reconnect was ever scheduled
		expect(controller.connectionState()).toBe('closed');
		expect(controller.entries()).toEqual([]);

		// Dispose during a pending reconnect cancels the one-shot timer.
		const second = createSessionChannelController({
			sessionId: SESSION_A,
			getCsrfToken: () => 'csrf-d07',
			socketFactory,
			reconnectDelaysMs: [10]
		});
		second.connect();
		await openChannel(sockets);
		sockets[1].serverClose(); // schedules a one-shot reconnect
		second.dispose(); // before it fires
		await delay(60);
		expect(sockets.length).toBe(2); // the timer never fired
		expect(controller.connectionState()).toBe('closed');
	});
});

interface SessionStreamDraftLike {
	stream_id: string;
	run_id: string | null;
	text: string;
	seen_delta_ids: Set<string>;
}

// ---------------------------------------------------------------------------
// Browser layer: the REAL client + REAL ChatDrawerSection in Chromium
// ---------------------------------------------------------------------------

const FIXTURE_URL = '/mai-session-client-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const VIRTUAL_ID = 'virtual:mai-session-client-entry';
const DRAWER = `${FRONTEND_ROOT}src/lib/workspace/ChatDrawerSection.svelte`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const LEGACY_RUN_ID = '22222222-2222-4222-8222-222222222222';
const CONVERSATION_ID = '33333333-3333-4333-8333-333333333333';

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

declare global {
	interface Window {
		__maiSessionState?: {
			calls: Array<{ method: string; url: string; body: unknown; csrf: string | null }>;
			renameResult: string | null;
		};
	}
}

interface DrawerFixtureTag {
	tag_id: string;
	name: string;
	assigned_by: string;
	assignment_source: string;
	created_at: string;
}

function harnessEntry(): string {
	return `
import { mount } from 'svelte';
import ChatDrawerSection from ${JSON.stringify(DRAWER)};
import { createMaiApiClient } from ${JSON.stringify(CLIENT)};
import ${JSON.stringify(TOKENS)};

const SESSION_ID = ${JSON.stringify(SESSION_ID)};
const LEGACY_RUN_ID = ${JSON.stringify(LEGACY_RUN_ID)};
const CONVERSATION_ID = ${JSON.stringify(CONVERSATION_ID)};
const SERVER_A = ${JSON.stringify(SERVER_A)};

const state = { calls: [], renameResult: null };
window.__maiSessionState = state;

const tag = (n) => ({
  tag_id: 'aaaaaaaa-aaaa-4aaa-8aaa-' + String(n).padStart(12, '0'),
  name: 'tag-' + String(n).padStart(2, '0'),
  assigned_by: '',
  assignment_source: 'system',
  created_at: '2026-01-01T00:00:00Z',
});

const record = (method, url, body, csrf) => {
  state.calls.push({ method, url, body, csrf });
};

// Recording transport: one canned route table + the call log the tests
// assert against. Canned bodies are minimal but type-honest.
async function recordingFetch(url, init) {
  const method = (init?.method ?? 'GET').toUpperCase();
  const body = init?.body ? JSON.parse(init.body) : null;
  const csrf = (init?.headers?.['x-csrf-token']) ?? null;
  record(method, String(url), body, csrf);
  const path = String(url).split('?')[0];
  const query = new URL(String(url), 'http://fixture.local').searchParams;

  if (method === 'PATCH' && path === '/api/v1/agent-sessions/' + SESSION_ID) {
    return new Response(JSON.stringify({ session_id: SESSION_ID, server_id: SERVER_A, source: 'interactive', status: 'idle', title: body.title, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-02T00:00:00Z' }), { status: 200, headers: { 'content-type': 'application/json' } });
  }
  if (method === 'PATCH' && path === '/api/v1/chat/runs/' + LEGACY_RUN_ID) {
    return new Response(JSON.stringify({ run_id: LEGACY_RUN_ID, server_id: SERVER_A, title: body.title, state: 'succeeded', service_tags: [], tag_preview: { preview: [], total: 0 }, submitted_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-02T00:00:00Z' }), { status: 200, headers: { 'content-type': 'application/json' } });
  }
  if (method === 'GET' && path === '/api/v1/agent-sessions/' + SESSION_ID + '/tags') {
    const offset = Number(query.get('offset') ?? 0);
    const limit = Number(query.get('limit') ?? 10);
    const all = Array.from({ length: 12 }, (_, i) => tag(i));
    if (offset >= 10) all.push(tag(9)); // page two re-serves page one's last row
    const items = all.slice(offset, offset + limit);
    return new Response(JSON.stringify({ limit, offset, total: 12, has_more: offset + limit < 12, items }), { status: 200, headers: { 'content-type': 'application/json' } });
  }
  if (method === 'GET' && path === '/api/v1/chat/conversations/' + CONVERSATION_ID + '/tags') {
    return new Response(JSON.stringify({ limit: 10, offset: Number(query.get('offset') ?? 0), total: 3, has_more: false, items: [tag(20), tag(21), tag(22)] }), { status: 200, headers: { 'content-type': 'application/json' } });
  }
  return new Response(JSON.stringify({ error: { code: 'not_found', message: 'no fixture route', request_id: 'req', fields: {} } }), { status: 404, headers: { 'content-type': 'application/json' } });
}

const client = createMaiApiClient({ fetch: recordingFetch, getCsrfToken: () => 'csrf-d07' });

const rows = [
  {
    run_id: SESSION_ID,
    session_id: SESSION_ID,
    server_id: SERVER_A,
    title: 'Alpha session',
    status: 'idle',
    updated_at: '2026-01-01T00:00:00Z',
    service_tags: [],
    tag_preview: { preview: ['alpha', 'beta', 'gamma'], total: 12 }
  },
  {
    run_id: LEGACY_RUN_ID,
    conversation_id: CONVERSATION_ID,
    server_id: SERVER_A,
    title: 'Legacy run',
    state: 'succeeded',
    service_tags: ['web'],
    tag_preview: { preview: ['ops'], total: 3 }
  }
];

// Identity-routed tag adapter (the DesktopShell composition, verbatim):
// session_id rows read the agent-session route, conversation rows the
// conversation route, bare run rows the job route.
function tagLoader(row) {
  if (typeof row.session_id === 'string' && row.session_id !== '') {
    return async (offset, limit) => {
      const page = await client.listAgentSessionTags(row.session_id, row.server_id, { offset, limit });
      return { limit: page.limit, offset: page.offset, total: page.total, has_more: page.has_more, items: (page.items ?? []).map((one) => ({ tag_id: one.tag_id, name: one.name })) };
    };
  }
  if (typeof row.conversation_id === 'string' && row.conversation_id !== '') {
    return async (offset, limit) => {
      const page = await client.listConversationTags(row.conversation_id, row.server_id, { offset, limit });
      return { limit: page.limit, offset: page.offset, total: page.total, has_more: page.has_more, items: (page.items ?? []).map((one) => ({ tag_id: one.tag_id, name: one.name })) };
    };
  }
  return null;
}

async function rename(rowKey, title) {
  const row = rows.find((one) => (one.session_id ?? one.run_id) === rowKey);
  if (row !== undefined && typeof row.session_id === 'string' && row.session_id !== '') {
    const view = await client.renameAgentSession(row.session_id, { title });
    state.renameResult = view.title;
    return;
  }
  if (row !== undefined && typeof row.conversation_id === 'string' && row.conversation_id !== '') {
    await client.renameConversation(row.run_id, { title });
    state.renameResult = title;
    return;
  }
  state.renameResult = 'unrouted';
}

mount(ChatDrawerSection, {
  target: document.getElementById('app'),
  props: {
    conversations: rows,
    totalConversations: 15,
    hasMoreConversations: false,
    loadingMoreConversations: false,
    activeRunId: null,
    onnewchat: () => {},
    onselectconversation: () => {},
    onrenameconversation: (rowKey, title) => rename(rowKey, title),
    tagLoader
  }
});
`;
}

let harnessJs = '';
let harnessCss = '';

function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-session-client-harness',
		resolveId(id) {
			if (id === VIRTUAL_ID) return `\0${VIRTUAL_ID}`;
			return undefined;
		},
		load(id) {
			if (id === `\0${VIRTUAL_ID}`) return code;
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(harnessEntry())],
		resolve: {
			alias: {
				// The component tree uses the SvelteKit `$lib` alias; the plain
				// in-memory build maps it to the same directory.
				$lib: `${FRONTEND_ROOT}src/lib`
			}
		},
		build: {
			write: false,
			emptyOutDir: true,
			outDir: 'node_modules/.mai-session-client-fixture',
			rollupOptions: {
				input: VIRTUAL_ID,
				output: { entryFileNames: 'demo.js', assetFileNames: 'demo[extname]' }
			}
		}
	});

	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') harnessJs += item.code;
			else if (item.fileName.endsWith('.css')) harnessCss += String(item.source);
		}
	}
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete: missing JS chunk or CSS asset');
	}
});

test.beforeEach(async ({ page }) => {
	await page.mouse.move(200, 560);

	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAI session-client fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body style="padding-top:24px">
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script>
</body>
</html>`;

	await page.route('**/*', (route: Route) => {
		const { pathname } = new URL(route.request().url());
		if (!pathname.startsWith(FIXTURE_URL)) return route.fulfill({ contentType: 'text/html', body: html });
		if (pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (pathname.endsWith('demo.css')) {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		return route.fulfill({ contentType: 'text/html', body: html });
	});

	await page.goto(FIXTURE_URL);
	await page.mouse.move(200, 560);
	await page.mouse.down();
	await page.mouse.up();
	await expect(page.getByTestId('drawer-history-item')).toHaveCount(2);
});

test('session rename routes the PATCH to the agent-session identity', async ({ page }) => {
	const items = page.getByTestId('drawer-history-item');
	await expect(items.first()).toContainText('Alpha session');

	// The session row is first: open its inline rename editor. The rename
	// button is a sibling of the NavItem, so target the button list itself.
	await page.getByTestId('drawer-history-rename').first().click();
	const input = page.getByTestId('drawer-history-rename-input');
	await expect(input).toBeVisible();
	await input.fill('Renamed over session id');
	await input.press('Enter');

	await expect
		.poll(() => page.evaluate(() => window.__maiSessionState?.renameResult ?? null))
		.toBe('Renamed over session id');
	const calls = await page.evaluate(() => window.__maiSessionState?.calls ?? []);
	const rename = calls.find(
		(call) => call.method === 'PATCH' && call.url.includes(`/api/v1/agent-sessions/${SESSION_ID}`)
	);
	expect(rename).toBeDefined();
	expect(rename?.body).toEqual({ title: 'Renamed over session id' });
	// CSRF double submit rides the mutation (C08 rule 2).
	expect(rename?.csrf).toBe('csrf-d07');
});

test('legacy rows keep their reviewed route while dual-wired', async ({ page }) => {
	const items = page.getByTestId('drawer-history-item');
	await page.getByTestId('drawer-history-rename').nth(1).click();
	const input = page.getByTestId('drawer-history-rename-input');
	await input.fill('Renamed legacy');
	await input.press('Enter');

	await expect
		.poll(() => page.evaluate(() => window.__maiSessionState?.renameResult ?? null))
		.toBe('Renamed legacy');
	const calls = await page.evaluate(() => window.__maiSessionState?.calls ?? []);
	const rename = calls.find(
		(call) => call.method === 'PATCH' && call.url.includes(`/api/v1/chat/runs/${LEGACY_RUN_ID}`)
	);
	expect(rename).toBeDefined();
	expect(rename?.body).toEqual({ title: 'Renamed legacy' });
});

test('HistoryTags pagination rides the agent-session tag route, deduplicated and honest', async ({
	page
}) => {
	const trigger = page.getByTestId('drawer-history-row-tags-trigger').first();
	await trigger.hover();
	const pop = page.getByTestId('drawer-history-row-tags-pop').first();
	await expect(pop).toBeVisible();

	// First page: the bounded 10-tag slice of the 12-tag total.
	const rows = pop.getByTestId('drawer-history-row-tags-row');
	await expect(rows).toHaveCount(10);
	const sessionTagsCalls = (
		await page.evaluate(() => window.__maiSessionState?.calls ?? [])
	).filter((call) => call.url.includes(`/api/v1/agent-sessions/${SESSION_ID}/tags`));
	expect(sessionTagsCalls).toHaveLength(1);
	// The client records relative paths (same-origin boundary); parse with a
	// base to assert the bounded query string honestly.
	const firstUrl = new URL(sessionTagsCalls[0].url, 'http://fixture.local');
	expect(firstUrl.searchParams.get('server_id')).toBe(SERVER_A);
	expect(firstUrl.searchParams.get('limit')).toBe('10');
	expect(firstUrl.searchParams.get('offset')).toBe('0');
	await expect(pop.getByTestId('drawer-history-row-tags-count')).toContainText('10 of 12');

	// Load more: the second page re-serves one row — deduplicated client-side
	// (10 + tags 10..11 unique + the re-served tag-09 = 12 rows).
	await pop.getByTestId('drawer-history-row-tags-load-more').click();
	await expect(rows).toHaveCount(12);
	const calls = await page.evaluate(() => window.__maiSessionState?.calls ?? []);
	const sessionTags = calls.filter((call) =>
		call.url.includes(`/api/v1/agent-sessions/${SESSION_ID}/tags`)
	);
	expect(sessionTags).toHaveLength(2);
	const secondUrl = new URL(sessionTags[1].url, 'http://fixture.local');
	expect(secondUrl.searchParams.get('offset')).toBe('10');
	await expect(pop.getByTestId('drawer-history-row-tags-count')).toContainText('12 of 12');
	// has_more flipped honestly: no third load control remains.
	await expect(pop.getByTestId('drawer-history-row-tags-load-more')).toHaveCount(0);
});

test('the legacy row keeps the conversation tag route while dual-wired', async ({ page }) => {
	const trigger = page.getByTestId('drawer-history-row-tags-trigger').nth(1);
	await trigger.hover();
	const pop = page.getByTestId('drawer-history-row-tags-pop').nth(1);
	await expect(pop).toBeVisible();
	await expect(pop.getByTestId('drawer-history-row-tags-row')).toHaveCount(3);
	const calls = await page.evaluate(() => window.__maiSessionState?.calls ?? []);
	const legacy = calls.find((call) =>
		call.url.includes(`/api/v1/chat/conversations/${CONVERSATION_ID}/tags`)
	);
	expect(legacy).toBeDefined();
	expect(legacy?.method).toBe('GET');
});
