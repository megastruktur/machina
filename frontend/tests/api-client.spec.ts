import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import {
	ApiError,
	createMaiApiClient,
	MaiClientError,
	readMaiEvent,
	readNotificationWakeup,
	subscribeEvents,
	subscribeNotificationEvents,
	type NotificationListQuery,
	type NotificationWakeup,
	type SseFrame
} from '../src/lib/api/client';
import type { components, paths } from '../src/lib/api/schema.generated';

/** Node globals (ambient: the project tsconfig targets browser-first code). */
declare const process: { env: Record<string, string | undefined> };

/**
 * 02-api-client — focused proofs for the typed API client boundary
 * (frontend/src/lib/api/client.ts over src/lib/api/schema.generated.ts).
 *
 * Three layers, one file (focused per test policy; no full journey here —
 * that is the 02-verify assembler's scenario):
 *
 * 1. Boundary transport (Node, injected fetch): CSRF double-submit on
 *    mutations only, fail-closed without a token, C01 envelope → typed
 *    ApiError, non-envelope collapse, explicit per-call server target with
 *    zero client-level state, SSE replay-cursor parsing.
 * 1b. Notification inbox surface (Node, injected fetch): canonical filter
 *    shapes incl. server_id|scope=global mutual exclusion (fails closed),
 *    unread-count/mark-all-read/ack/unack/per-server-unacknowledge, CSRF on
 *    every mutation, ingest-key lifecycle (metadata page, one-time plaintext
 *    mint receipt, revoke) and the wakeup-only SSE binding (identity/resync
 *    frames, no Last-Event-ID, refetch hook on stream end).
 * 2. Browser semantics (real Chromium, preview origin, route-mocked API):
 *    HttpOnly `mai_session` never readable by JS, `mai_csrf` readable and
 *    attached, nothing in web storage, envelope errors and SSE frames flow
 *    through the real module in the page.
 * 3. Real boundary (env-gated, MAI_E2E_API_URL): the same client module
 *    against a live `uvicorn mai.api.app:create_app` fixture — bootstrap/
 *    login/profile CRUD/409 conflict plus the CSRF/origin negative controls.
 *    Skipped unless the fixture URL is provided; the evidence README
 *    documents the exact fixture commands.
 *
 * Browser layer uses the sibling-spec convention: an IN-MEMORY Vite
 * production build of a virtual entry module importing the real client.ts,
 * served through route interception (reviewed 01-web component-fixture
 * pattern, tests/window-drag.spec.ts).
 */

// ---------------------------------------------------------------------------
// Compile-time pins: client payload types ARE generated-schema types.
// (Checked by `pnpm check`; no runtime cost — interfaces erase.)
// ---------------------------------------------------------------------------

type Assert<T extends true> = T;
type LoginBody = paths['/api/v1/auth/login']['post']['requestBody']['content']['application/json'];
type _LoginBodyIsGenerated = Assert<LoginBody extends components['schemas']['PasswordBody'] ? true : false>;
type CreateBody = paths['/api/v1/servers']['post']['requestBody']['content']['application/json'];
type _CreateBodyIsGenerated = Assert<CreateBody extends components['schemas']['ProfileCreate'] ? true : false>;
type ProfileResult = paths['/api/v1/servers']['post']['responses'][201]['content']['application/json'];
type _ResultIsGenerated = Assert<
	ProfileResult extends components['schemas']['ProfileMutationResult'] ? true : false
>;
type TestBody = paths['/api/v1/servers/onboarding/test']['post']['requestBody']['content']['application/json'];
type _TestBodyIsGenerated = Assert<
	TestBody extends components['schemas']['OnboardingTestRequest'] ? true : false
>;
type EnrollBody = paths['/api/v1/servers/onboarding/enroll']['post']['requestBody']['content']['application/json'];
type _EnrollBodyIsGenerated = Assert<
	EnrollBody extends components['schemas']['OnboardingEnrollRequest'] ? true : false
>;
type ControllerKey = paths['/api/v1/servers/onboarding/controller-key']['get']['responses'][200]['content']['application/json'];
type _ControllerKeyIsGenerated = Assert<
	ControllerKey extends components['schemas']['ControllerKeyResponse'] ? true : false
>;
// Notification inbox surface: the canonical query interfaces are hand-mapped
// mirrors of the generated route queries (assignability pinned both-ways
// below via the routes), and every payload/response alias is generated.
type InboxListQuery = NonNullable<paths['/api/v1/notifications']['get']['parameters']['query']>;
type _InboxListQueryIsGenerated = Assert<InboxListQuery extends NotificationListQuery ? true : false>;
type InboxListResponse = paths['/api/v1/notifications']['get']['responses'][200]['content']['application/json'];
type _InboxListResponseIsGenerated = Assert<
	InboxListResponse extends components['schemas']['NotificationPage'] ? true : false
>;
type UnreadCountResponse = paths['/api/v1/notifications/unread-count']['get']['responses'][200]['content']['application/json'];
type _UnreadCountResponseIsGenerated = Assert<
	UnreadCountResponse extends components['schemas']['UnreadCountView'] ? true : false
>;
type MarkAllReadResponse = paths['/api/v1/notifications/mark-all-read']['post']['responses'][200]['content']['application/json'];
type _MarkAllReadResponseIsGenerated = Assert<
	MarkAllReadResponse extends components['schemas']['MarkAllReadResult'] ? true : false
>;
type CreateIngestKeyBodyWire = paths['/api/v1/ingest-keys']['post']['requestBody']['content']['application/json'];
type _CreateIngestKeyBodyIsGenerated = Assert<
	CreateIngestKeyBodyWire extends components['schemas']['CreateIngestKeyBody'] ? true : false
>;
type IssuedKeyResponse = paths['/api/v1/ingest-keys']['post']['responses'][201]['content']['application/json'];
type _IssuedKeyResponseIsGenerated = Assert<
	IssuedKeyResponse extends components['schemas']['IssuedIngestKeyView'] ? true : false
>;

// ---------------------------------------------------------------------------
// Node boundary layer — injected-fetch stubs
// ---------------------------------------------------------------------------

interface CapturedRequest {
	url: string;
	method: string;
	headers: Record<string, string>;
	body: string | null;
	credentials: RequestCredentials | undefined;
}

type FetchHandler = (request: Request) => Response | Promise<Response>;

/** Origin the stub resolves relative client paths against (Node has no base). */
const STUB_ORIGIN = 'http://mai.test';

/** Recording fetch stub: captures each exchange, delegates to the handler. */
function stubFetch(handler: FetchHandler): { fetch: typeof fetch; calls: CapturedRequest[] } {
	const calls: CapturedRequest[] = [];
	const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
		// Node's Request rejects relative URLs; resolve against a stub origin.
		const resolved = input instanceof Request ? input : new Request(new URL(input.toString(), STUB_ORIGIN), init);
		const captured: CapturedRequest = {
			url: resolved.url,
			method: resolved.method,
			headers: Object.fromEntries(resolved.headers.entries()),
			body: resolved.method === 'GET' ? null : await resolved.clone().text(),
			credentials: init?.credentials
		};
		const response = await handler(resolved);
		calls.push(captured);
		return response;
	}) as typeof fetch;
	return { fetch: impl, calls };
}

function jsonResponse(status: number, body: unknown, headers?: Record<string, string>): Response {
	return new Response(body === undefined ? null : JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json', ...headers }
	});
}

const ENVELOPE_CONFLICT = {
	error: {
		code: 'conflict',
		message: 'the profile changed since the last observed revision',
		request_id: 'req-7f3a',
		fields: {}
	}
} as const;

test.describe('typed boundary transport (injected fetch)', () => {
	test('mutations carry the CSRF double-submit header; safe methods do not', async () => {
		const { fetch, calls } = stubFetch((request) => {
			if (request.method === 'GET') return jsonResponse(200, { status: 'ok' });
			// bootstrap 201 (setup-state) vs session mutations 204
			return new Response(null, { status: request.url.includes('/bootstrap') ? 201 : 204 });
		});
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 'csrf-token-1' });

		await client.login({ password: 'pw' });
		await client.health();

		const login = calls[0];
		expect(login.method).toBe('POST');
		expect(login.url).toContain('/api/v1/auth/login');
		expect(login.credentials).toBe('include');
		expect(login.headers['x-csrf-token']).toBe('csrf-token-1');
		expect(login.headers['content-type']).toBe('application/json');
		expect(JSON.parse(login.body ?? '')).toEqual({ password: 'pw' });
		// Safe method: session cookie is the only credential layer (auth ADR §5).
		expect(calls[1].method).toBe('GET');
		expect(calls[1].url).toContain('/api/v1/health');
		expect(calls[1].headers['x-csrf-token']).toBeUndefined();
	});

	test('mutations fail closed before any byte when no CSRF token exists', async () => {
		let networkCalls = 0;
		const { fetch } = stubFetch((request) => {
			networkCalls += 1;
			// bootstrap 201 (setup-state) vs session mutations 204
			return new Response(null, { status: request.url.includes('/bootstrap') ? 201 : 204 });
		});
		const client = createMaiApiClient({ fetch, getCsrfToken: () => null });

		await expect(client.logout()).rejects.toThrow(/no mai_csrf token/);
		expect(networkCalls, 'no session mutation may leave without a token').toBe(0);

		// G1 setup-state routes are exact-origin-protected, not CSRF-protected
		// (auth ADR §3/§8): they must go through even with an empty cookie jar.
		await expect(client.bootstrap({ password: 'pw' })).resolves.toBeUndefined();
		expect(networkCalls).toBe(1);
	});

	test('C01 envelopes become ApiError with stable code, id and fields', async () => {
		const { fetch } = stubFetch((request) => {
			if (request.method === 'PUT') return jsonResponse(409, ENVELOPE_CONFLICT);
			return jsonResponse(422, {
				error: {
					code: 'validation_failed',
					message: 'profile document failed validation',
					request_id: 'req-91bc',
					fields: { 'ssh.host_key': 'invalid_field' }
				}
			});
		});
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 't' });

		const conflict = await client
			.editProfile('8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f', {
				name: 'x',
				prompt_revision: 1,
				environment: 'linux_ssh',
				ssh: FIXTURE_SSH,
				privilege: 'none',
				capabilities: [],
				expected_revision: 1
			})
			.catch((error: unknown) => error);
		expect(conflict).toBeInstanceOf(ApiError);
		expect((conflict as ApiError).status).toBe(409);
		expect((conflict as ApiError).code).toBe('conflict');
		expect((conflict as ApiError).requestId).toBe('req-7f3a');

		const validation = await client.createProfile(VALID_CREATE).catch((error: unknown) => error);
		expect((validation as ApiError).status).toBe(422);
		expect((validation as ApiError).code).toBe('validation_failed');
		expect((validation as ApiError).fields).toEqual({ 'ssh.host_key': 'invalid_field' });
	});

	test('non-envelope error bodies collapse to generic request_rejected', async () => {
		const { fetch } = stubFetch(
			() => new Response('<html>gateway error</html>', { status: 502 })
		);
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 't' });

		const error = await client.refreshCsrf().catch((error: unknown) => error);
		expect(error).toBeInstanceOf(ApiError);
		expect((error as ApiError).status).toBe(502);
		expect((error as ApiError).code).toBe('request_rejected');
		expect((error as ApiError).message).toContain('internal server error');
		// raw body text must never surface to caller code
		expect((error as ApiError).message).not.toContain('gateway');
	});

	test('server target is always the explicit argument; instances share no state', async () => {
		const { fetch, calls } = stubFetch(() =>
			jsonResponse(200, {
				schema_version: 1,
				server_id: '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f',
				name: 'alpha',
				prompt_revision: 1,
				environment: 'linux_ssh',
				ssh: FIXTURE_SSH,
				privilege: 'none',
				capabilities: [],
				revision: 1,
				sha256: 'ab'.repeat(32),
				projection: { revision: 1, stale: false }
			})
		);
		const alpha = createMaiApiClient({ fetch, baseUrl: 'http://alpha.internal' });
		const beta = createMaiApiClient({ fetch });

		await alpha.readProfile('8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f');
		await beta.readProfile('44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21');

		expect(calls[0].url).toBe('http://alpha.internal/api/v1/servers/8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f');
		expect(calls[1].url).toBe(`${STUB_ORIGIN}/api/v1/servers/44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21`);
	});

	test('subscribeEvents parses SSE frames and carries the replay cursor', async () => {
		let requestedCursor: string | null = null;
		const stream = [
			': heartbeat comment\n\n',
			'id: 43\nevent: run\ndata: {"seq":43,"run_id":"r1","ts":"2026-10-14T00:00:00Z","type":"step.completed","v":1,"data":{"exit":0}}\n\n',
			'id: 44\r\ndata: first\ndata: second\n\n'
		].join('');
		const { fetch } = stubFetch((request) => {
			requestedCursor = request.headers.get('last-event-id');
			return new Response(stream, {
				status: 200,
				headers: { 'content-type': 'text/event-stream' }
			});
		});

		const frames: SseFrame[] = [];
		await subscribeEvents('http://mai.test/api/v1/events', {
			fetch,
			cursor: '42',
			onEvent: (frame) => frames.push(frame)
		});

		expect(requestedCursor).toBe('42');
		expect(frames).toHaveLength(2);
		const event = readMaiEvent(frames[0]);
		expect(event).not.toBeNull();
		expect(event?.seq).toBe(43);
		expect(event?.type).toBe('step.completed');
		expect(event?.data).toEqual({ exit: 0 });
		// non-JSON data is delivered as a frame, but is not a MaiRunEvent
		expect(frames[1].data).toBe('first\nsecond');
		expect(readMaiEvent(frames[1])).toBeNull();
	});

	test('event-stream denials surface the same typed ApiError', async () => {
		const { fetch } = stubFetch(() => jsonResponse(401, ENVELOPE_CONFLICT));
		await expect(
			subscribeEvents('http://mai.test/api/v1/events', {
				fetch,
				onEvent: () => {
					throw new Error('no frame may arrive on a denial');
				}
			})
		).rejects.toThrow('API 401 conflict');
	});

	test('onboarding surface: controller-key GET, test/enroll POST with CSRF, no credential_ref', async () => {
		const { fetch, calls } = stubFetch((request) => {
			const path = new URL(request.url).pathname;
			if (path === '/api/v1/servers/onboarding/controller-key') {
				return jsonResponse(200, {
					public_key: 'ssh-ed25519 AAAA-fixture mai-controller',
					key_type: 'ssh-ed25519',
					fingerprint: 'SHA256:fixture'
				});
			}
			if (path === '/api/v1/servers/onboarding/test') {
				return jsonResponse(200, {
					candidates: [{ host_key: 'SHA256:a', key_type: 'ssh-ed25519' }]
				});
			}
			if (path === '/api/v1/servers/onboarding/enroll') {
				return jsonResponse(201, {
					server_id: '0d0a1b2c-3d4e-4f5a-6b7c-8d9e0f1a2b3c',
					revision: 1,
					schema_revision: 1,
					sha256: 'c'.repeat(64)
				});
			}
			return jsonResponse(404, {
				error: { code: 'not_found', message: 'no such route', request_id: 'req-x', fields: {} }
			});
		});
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 'csrf-token-9' });

		// GET: safe method — no CSRF header, exact typed answer.
		const key = await client.readControllerKey();
		expect(key).toEqual({
			public_key: 'ssh-ed25519 AAAA-fixture mai-controller',
			key_type: 'ssh-ed25519',
			fingerprint: 'SHA256:fixture'
		});

		// POST discovery (host_key: null) + POST confirmed enroll.
		const discovery = await client.testOnboardingConnection({
			ssh: { host: 't.example.internal', port: 22, user: 'deploy' },
			privilege: 'sudo',
			host_key: null,
			timeout_seconds: 10
		});
		expect(discovery).toEqual({
			candidates: [{ host_key: 'SHA256:a', key_type: 'ssh-ed25519' }]
		});
		const enrolled = await client.enrollViaOnboarding({
			name: 'fixture',
			prompt_revision: 1,
			prompt_text: 'fixture prompt',
			environment: 'linux_ssh',
			privilege: 'sudo',
			capabilities: ['host.shell'],
			ssh: { host: 't.example.internal', port: 22, user: 'deploy' },
			host_key: 'SHA256:a',
			host_key_confirmed: true,
			timeout_seconds: 10
		});
		expect(enrolled).toMatchObject({ revision: 1 });

		const [keyCall, testCall, enrollCall] = calls;
		expect(keyCall.method).toBe('GET');
		expect(keyCall.headers['x-csrf-token']).toBeUndefined();
		expect(keyCall.url).toContain('/api/v1/servers/onboarding/controller-key');
		expect(testCall.method).toBe('POST');
		expect(testCall.headers['x-csrf-token']).toBe('csrf-token-9');
		expect(testCall.url).toContain('/api/v1/servers/onboarding/test');
		expect(enrollCall.method).toBe('POST');
		expect(enrollCall.url).toContain('/api/v1/servers/onboarding/enroll');
		expect(JSON.parse(enrollCall.body ?? '{}')).not.toHaveProperty('credential_ref');

		// Structured 409 on the new surface maps to the same typed ApiError.
		const conflicting = createMaiApiClient({
			fetch: async () => jsonResponse(409, ENVELOPE_CONFLICT),
			getCsrfToken: () => 'csrf-token-9'
		});
		await expect(
			conflicting.testOnboardingConnection({
				ssh: { host: 't.example.internal', port: 22, user: 'deploy' },
				privilege: 'sudo',
				host_key: 'SHA256:stale',
				timeout_seconds: 10
			})
		).rejects.toMatchObject({ status: 409, code: 'conflict' });
	});
});

// ---------------------------------------------------------------------------
// Notification inbox surface (injected fetch) — canonical filters, CSRF,
// envelope collapse, ingest-key lifecycle and the wakeup-only SSE binding.
// ---------------------------------------------------------------------------

test.describe('notification inbox surface (injected fetch)', () => {
	const SERVER = '44b05a60-1e2b-4c3d-9a8b-7c6d5e4f3a21';
	const ALERT = 'a1dd2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
	const KEY_ID = '0d0a1b2c-3d4e-4f5a-6b7c-8d9e0f1a2b3c';

	/** Synthetic notice fixture; the shape is the generated AlertView. */
	const fixtureAlert = (acked: boolean): components['schemas']['AlertView'] => ({
		alert_id: ALERT,
		server_id: null,
		origin: 'external',
		dedup_key: 'external:fixture',
		severity: 'warning',
		title: 'disk almost full',
		summary: null,
		source: 'host-monitor',
		body: '/var usage 94% on db-1',
		context_ref: null,
		delivery_status: 'delivered',
		created_at: '2026-11-14T00:00:00Z',
		delivered_at: '2026-11-14T00:00:01Z',
		acknowledged_at: acked ? '2026-11-14T00:05:00Z' : null,
		acked,
		finding_id: null,
		run_id: null
	});

	/** SSE response with the exact producer content type. */
	function sseResponse(body: string): Response {
		return new Response(body, {
			status: 200,
			headers: { 'content-type': 'text/event-stream' }
		});
	}

	function inboxClient(doFetch: typeof globalThis.fetch) {
		return createMaiApiClient({ fetch: doFetch, getCsrfToken: () => 'csrf-token-nbi' });
	}

	test('canonical list filters produce exactly the frozen query params', async () => {
		const { fetch, calls } = stubFetch(() =>
			jsonResponse(200, { alerts: [fixtureAlert(false)], limit: 10, offset: 20, total: 1, has_more: false })
		);
		const client = inboxClient(fetch);

		const page = await client.listInboxNotifications({
			server_id: SERVER,
			include_global: false,
			unread: true,
			origin: 'external',
			source: 'host-monitor',
			q: 'disk',
			limit: 10,
			offset: 20
		});
		expect(page.total).toBe(1);

		const url = new URL(calls[0].url);
		expect(url.pathname).toBe('/api/v1/notifications');
		expect(url.searchParams.get('server_id')).toBe(SERVER);
		expect(url.searchParams.get('include_global')).toBe('false');
		expect(url.searchParams.get('unread')).toBe('true');
		expect(url.searchParams.get('origin')).toBe('external');
		expect(url.searchParams.get('source')).toBe('host-monitor');
		expect(url.searchParams.get('q')).toBe('disk');
		expect(url.searchParams.get('limit')).toBe('10');
		expect(url.searchParams.get('offset')).toBe('20');
		expect(calls[0].method).toBe('GET');
		expect(calls[0].credentials).toBe('include');
		// Safe method: no CSRF header ever rides a read.
		expect(calls[0].headers['x-csrf-token']).toBeUndefined();

		// Global-only selection: scope=global, NEVER server_id beside it, and
		// include_global is meaningless there so it never travels (frozen
		// semantics: only meaningful beside an explicit server_id).
		await client.listInboxNotifications({ scope: 'global', include_global: true, unread: false });
		const globalUrl = new URL(calls[1].url);
		expect(globalUrl.searchParams.get('scope')).toBe('global');
		expect(globalUrl.searchParams.has('server_id')).toBe(false);
		expect(globalUrl.searchParams.has('include_global')).toBe(false);
		expect(globalUrl.searchParams.get('unread')).toBe('false');

		// No filters: the bare collection route (ALL notices server-side).
		await client.listInboxNotifications();
		expect(new URL(calls[2].url).search).toBe('');
	});

	test('server_id and scope=global together fail closed before any byte', async () => {
		let networkCalls = 0;
		const { fetch } = stubFetch(() => {
			networkCalls += 1;
			return jsonResponse(200, { alerts: [], limit: 50, offset: 0, total: 0, has_more: false });
		});
		const client = inboxClient(fetch);

		await expect(
			client.listInboxNotifications({ server_id: SERVER, scope: 'global' })
		).rejects.toThrow(MaiClientError);
		await expect(
			client.getUnreadNotificationCount({ server_id: SERVER, scope: 'global' })
		).rejects.toThrow(/mutually exclusive/);
		await expect(
			client.markAllNotificationsRead({ server_id: SERVER, scope: 'global' })
		).rejects.toThrow(/mutually exclusive/);
		expect(networkCalls, 'an invalid filter pair may not leave the client').toBe(0);
	});

	test('unread-count and mark-all-read mirror the canonical filter surface', async () => {
		const { fetch, calls } = stubFetch((request) => {
			if (new URL(request.url).pathname === '/api/v1/notifications/unread-count') {
				return jsonResponse(200, { total: 3 });
			}
			return jsonResponse(200, { updated: 3 });
		});
		const client = inboxClient(fetch);

		const count = await client.getUnreadNotificationCount({
			server_id: SERVER,
			include_global: true
		});
		expect(count).toEqual({ total: 3 });
		const countUrl = new URL(calls[0].url);
		expect(countUrl.pathname).toBe('/api/v1/notifications/unread-count');
		expect(countUrl.searchParams.get('server_id')).toBe(SERVER);
		expect(countUrl.searchParams.get('include_global')).toBe('true');

		const receipt = await client.markAllNotificationsRead({
			server_id: SERVER,
			unread: true,
			q: 'disk'
		});
		expect(receipt).toEqual({ updated: 3 });
		const markUrl = new URL(calls[1].url);
		expect(markUrl.pathname).toBe('/api/v1/notifications/mark-all-read');
		expect(markUrl.searchParams.get('server_id')).toBe(SERVER);
		expect(markUrl.searchParams.get('unread')).toBe('true');
		expect(markUrl.searchParams.get('q')).toBe('disk');
		// Mutation: session cookie jar + CSRF double submit, no body dialect.
		expect(calls[1].method).toBe('POST');
		expect(calls[1].body).toBe('');
		expect(calls[1].credentials).toBe('include');
		expect(calls[1].headers['x-csrf-token']).toBe('csrf-token-nbi');
	});

	test('ack/unack and per-server unacknowledge carry CSRF; envelopes collapse to ApiError', async () => {
		const { fetch, calls } = stubFetch((request) => {
			const path = new URL(request.url).pathname;
			if (path === `/api/v1/notifications/${ALERT}/ack`) {
				return jsonResponse(200, { alert: fixtureAlert(true) });
			}
			if (path === `/api/v1/notifications/${ALERT}/unack`) {
				return jsonResponse(200, { alert: fixtureAlert(false) });
			}
			if (path === `/api/v1/monitoring/servers/${SERVER}/notifications/${ALERT}/unacknowledge`) {
				return jsonResponse(200, { alert: fixtureAlert(false) });
			}
			return jsonResponse(404, {
				error: {
					code: 'not_found',
					message: 'no notice for this id',
					request_id: 'req-nbi-404',
					fields: {}
				}
			});
		});
		const client = inboxClient(fetch);

		const acked = await client.acknowledgeInboxNotification(ALERT);
		expect(acked.alert.acked).toBe(true);
		expect(acked.alert.acknowledged_at).toBe('2026-11-14T00:05:00Z');
		expect(calls[0].method).toBe('POST');
		expect(new URL(calls[0].url).pathname).toBe(`/api/v1/notifications/${ALERT}/ack`);
		expect(calls[0].headers['x-csrf-token']).toBe('csrf-token-nbi');
		expect(calls[0].credentials).toBe('include');

		await client.unacknowledgeInboxNotification(ALERT);
		expect(new URL(calls[1].url).pathname).toBe(`/api/v1/notifications/${ALERT}/unack`);
		expect(calls[1].headers['x-csrf-token']).toBe('csrf-token-nbi');

		await client.unacknowledgeServerNotification(SERVER, ALERT);
		expect(new URL(calls[2].url).pathname).toBe(
			`/api/v1/monitoring/servers/${SERVER}/notifications/${ALERT}/unacknowledge`
		);
		expect(calls[2].headers['x-csrf-token']).toBe('csrf-token-nbi');

		// Value-free 404 collapses to the typed ApiError envelope.
		const missing = await client
			.acknowledgeInboxNotification('00000000-0000-0000-0000-000000000000')
			.catch((error: unknown) => error);
		expect(missing).toBeInstanceOf(ApiError);
		expect((missing as ApiError).status).toBe(404);
		expect((missing as ApiError).code).toBe('not_found');
		expect((missing as ApiError).requestId).toBe('req-nbi-404');
	});

	test('ingest-key surface: bounded page, mint with CSRF, revoke; plaintext only in the receipt', async () => {
		const PLAINTEXT = 'mai_ingest_fixture_never_real_000000000000000000000000';
		const { fetch, calls } = stubFetch((request) => {
			const path = new URL(request.url).pathname;
			if (request.method === 'GET' && path === '/api/v1/ingest-keys') {
				return jsonResponse(200, {
					keys: [
						{
							key_id: KEY_ID,
							label: 'host-monitor',
							server_id: SERVER,
							created_at: '2026-11-14T00:00:00Z',
							last_used_at: null,
							expires_at: null,
							revoked_at: null
						}
					],
					limit: 10,
					offset: 0,
					total: 1,
					has_more: false
				});
			}
			if (request.method === 'POST' && path === '/api/v1/ingest-keys') {
				return jsonResponse(201, {
					key: PLAINTEXT,
					key_id: KEY_ID,
					label: 'host-monitor',
					server_id: SERVER,
					created_at: '2026-11-14T00:00:00Z',
					last_used_at: null,
					expires_at: null,
					revoked_at: null
				});
			}
			if (path === `/api/v1/ingest-keys/${KEY_ID}/revoke`) {
				return jsonResponse(200, { key_id: KEY_ID, revoked: true });
			}
			return jsonResponse(404, {
				error: { code: 'not_found', message: 'no such route', request_id: 'req-x', fields: {} }
			});
		});
		const client = inboxClient(fetch);

		const page = await client.listIngestKeys({ limit: 10, offset: 0 });
		expect(page.keys).toHaveLength(1);
		expect(page.keys[0].label).toBe('host-monitor');
		expect(page.keys[0].server_id).toBe(SERVER);
		expect(page.has_more).toBe(false);
		// Metadata page: no key material field anywhere in the wire shape.
		expect(JSON.stringify(page)).not.toContain('key_hash');
		const listUrl = new URL(calls[0].url);
		expect(listUrl.pathname).toBe('/api/v1/ingest-keys');
		expect(listUrl.searchParams.get('limit')).toBe('10');
		expect(listUrl.searchParams.get('offset')).toBe('0');
		expect(calls[0].headers['x-csrf-token']).toBeUndefined();

		const issued = await client.createIngestKey({ label: 'host-monitor', server_id: SERVER });
		expect(issued.key).toBe(PLAINTEXT);
		expect(JSON.parse(calls[1].body ?? '{}')).toEqual({
			label: 'host-monitor',
			server_id: SERVER
		});
		expect(calls[1].method).toBe('POST');
		expect(calls[1].headers['x-csrf-token']).toBe('csrf-token-nbi');

		// Omitting server_id mints a GLOBAL key.
		await client.createIngestKey({ label: 'global-notifier' });
		expect(JSON.parse(calls[2].body ?? '{}')).toEqual({ label: 'global-notifier' });

		const revoked = await client.revokeIngestKey(KEY_ID);
		expect(revoked).toEqual({ key_id: KEY_ID, revoked: true });
		expect(calls[3].method).toBe('POST');
		expect(new URL(calls[3].url).pathname).toBe(`/api/v1/ingest-keys/${KEY_ID}/revoke`);
		expect(calls[3].headers['x-csrf-token']).toBe('csrf-token-nbi');
	});

	test('ingest-key create errors collapse to the envelope without echoing the payload', async () => {
		const { fetch } = stubFetch(() =>
			jsonResponse(422, {
				error: {
					code: 'validation_failed',
					message: 'key payload failed validation',
					request_id: 'req-nbi-422',
					fields: { label: 'too long' }
				}
			})
		);
		const client = inboxClient(fetch);
		const error = await client
			.createIngestKey({ label: 'x'.repeat(80) })
			.catch((e: unknown) => e);
		expect(error).toBeInstanceOf(ApiError);
		expect((error as ApiError).status).toBe(422);
		expect((error as ApiError).code).toBe('validation_failed');
		expect((error as ApiError).fields).toEqual({ label: 'too long' });
		// Raw body text never surfaces to caller code.
		expect((error as ApiError).message).not.toContain('x'.repeat(80));
	});

	test('wakeup frames narrow exactly; non-frames never reach the callback', async () => {
		expect(
			readNotificationWakeup({
				id: null,
				event: null,
				data: '{"kind":"created","alert_id":"a-1","server_id":null}'
			})
		).toEqual({ kind: 'created', alert_id: 'a-1', server_id: null });
		expect(
			readNotificationWakeup({
				id: null,
				event: null,
				data: '{"kind":"updated","alert_id":"a-2","server_id":"' + SERVER + '"}'
			})
		).toEqual({ kind: 'updated', alert_id: 'a-2', server_id: SERVER });
		expect(readNotificationWakeup({ id: null, event: null, data: '{"kind":"resync"}' })).toEqual({
			kind: 'resync'
		});
		// Unknown kinds, missing identity and non-JSON are not wakeups.
		expect(readNotificationWakeup({ id: null, event: null, data: '{"kind":"deleted"}' })).toBeNull();
		expect(readNotificationWakeup({ id: null, event: null, data: '{"kind":"created"}' })).toBeNull();
		expect(
			readNotificationWakeup({ id: null, event: null, data: '{"kind":"created","alert_id":"a-1"}' })
		).toBeNull();
		expect(readNotificationWakeup({ id: null, event: null, data: 'not-json' })).toBeNull();
	});

	test('subscribeNotificationEvents delivers identity/resync frames and never a cursor', async () => {
		const stream = [
			': ping\n\n',
			'data: {"kind":"created","alert_id":"a-1","server_id":null}\n\n',
			'data: {"kind":"updated","alert_id":"a-2","server_id":"' + SERVER + '"}\n\n',
			'data: not-json\n\n',
			'data: {"kind":"resync"}\n\n'
		].join('');
		const { fetch, calls } = stubFetch(() => sseResponse(stream));

		const wakeups: NotificationWakeup[] = [];
		let refetches = 0;
		await subscribeNotificationEvents('http://mai.test/api/v1/notifications/events', {
			fetch,
			onWakeup: (wakeup) => wakeups.push(wakeup),
			onStreamEnd: () => {
				refetches += 1;
			}
		});

		const request = calls[0];
		expect(new URL(request.url).pathname).toBe('/api/v1/notifications/events');
		expect(request.method).toBe('GET');
		expect(request.credentials).toBe('include');
		expect(request.headers['accept']).toBe('text/event-stream');
		// Wakeup-only: NO Last-Event-ID cursor ever travels on this stream.
		expect(request.headers['last-event-id']).toBeUndefined();

		expect(wakeups).toEqual([
			{ kind: 'created', alert_id: 'a-1', server_id: null },
			{ kind: 'updated', alert_id: 'a-2', server_id: SERVER },
			{ kind: 'resync' }
		]);
		// Bounded stream self-closed cleanly → refetch/resubscribe hook fired.
		expect(refetches).toBe(1);
	});

	test('client-bound subscription rides the instance base URL', async () => {
		const { fetch, calls } = stubFetch(() => sseResponse('data: {"kind":"resync"}\n\n'));
		const client = createMaiApiClient({
			fetch,
			baseUrl: 'http://alpha.internal',
			getCsrfToken: () => 'csrf-token-nbi'
		});
		const wakeups: NotificationWakeup[] = [];
		let refetches = 0;
		await client.subscribeNotificationEvents({
			onWakeup: (wakeup) => wakeups.push(wakeup),
			onStreamEnd: () => {
				refetches += 1;
			}
		});
		expect(calls[0].url).toBe('http://alpha.internal/api/v1/notifications/events');
		expect(wakeups).toEqual([{ kind: 'resync' }]);
		expect(refetches).toBe(1);
	});

	test('a caller abort suppresses the reconnect/refetch hook', async () => {
		const { fetch } = stubFetch(() => sseResponse(': ping\n\n'));
		const controller = new AbortController();
		controller.abort();
		let refetches = 0;
		await subscribeNotificationEvents('http://mai.test/api/v1/notifications/events', {
			fetch,
			signal: controller.signal,
			onWakeup: () => {
				throw new Error('no wakeup may arrive after an abort');
			},
			onStreamEnd: () => {
				refetches += 1;
			}
		});
		expect(refetches, 'aborted subscription must not fake a reconnect').toBe(0);
	});

	test('stream denials surface the same typed ApiError', async () => {
		const { fetch } = stubFetch(() => jsonResponse(401, ENVELOPE_CONFLICT));
		await expect(
			subscribeNotificationEvents('http://mai.test/api/v1/notifications/events', {
				fetch,
				onWakeup: () => {
					throw new Error('no frame may arrive on a denial');
				}
			})
		).rejects.toThrow('API 401 conflict');
	});
});

// ---------------------------------------------------------------------------
// Browser layer — real client module in real Chromium, route-mocked API
// ---------------------------------------------------------------------------

const FIXTURE_URL = '/mai-api-client-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;
const ENTRY_ID = 'virtual:mai-api-client-entry.js';

const ENTRY_SOURCE = `
import {
	createMaiApiClient,
	subscribeEvents,
	readMaiEvent,
	ApiError
} from '${CLIENT}';
window.__maiApiClientProbe = {
	createMaiApiClient,
	subscribeEvents,
	readMaiEvent,
	ApiError
};
`;

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

interface ProbeApi {
	createMaiApiClient: typeof createMaiApiClient;
	subscribeEvents: typeof subscribeEvents;
	readMaiEvent: typeof readMaiEvent;
	ApiError: typeof ApiError;
}

declare global {
	interface Window {
		/** Spec-unique probe global (no collision with the sibling specs). */
		__maiApiClientProbe?: ProbeApi;
	}
}

let harnessJs = '';

/** Serves the virtual entry module from the in-memory bundle. */
function harnessPlugin(entrySource: string): Plugin {
	return {
		name: 'mai-api-client-harness',
		resolveId(id) {
			if (id === ENTRY_ID) return `\0${id}`;
			return undefined;
		},
		load(id) {
			if (id === `\0${ENTRY_ID}`) return entrySource;
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [harnessPlugin(ENTRY_SOURCE)],
		build: {
			write: false,
			emptyOutDir: true,
			outDir: 'node_modules/.mai-api-client-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: { entryFileNames: 'demo.js', assetFileNames: 'demo[extname]' }
			}
		}
	});
	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	for (const bundle of outputs) {
		for (const item of bundle.output) {
			if (item.type === 'chunk') harnessJs += item.code;
		}
	}
	if (!harnessJs) throw new Error('harness bundle incomplete: missing JS chunk');
});

test.beforeEach(async ({ page }) => {
	const html = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>MAI api client fixture</title></head>
<body><div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script>
</body>
</html>`;

	await page.route('**/*', (route) => {
		const { pathname } = new URL(route.request().url());
		if (!pathname.startsWith(FIXTURE_URL)) return route.abort();
		if (pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
	});
});

/** Register an API mock; later routes win over the fixture catch-all. */
async function mockApi(page: Page, path: string, handler: (route: Route) => Promise<void>) {
	await page.route(`**${path}`, handler);
}

test.describe('browser session semantics (preview origin, mocked API)', () => {
	test('session cookie is HttpOnly-invisible, CSRF readable, storage empty, header flows', async ({
		page
	}) => {
		// The real wire behavior (login's Set-Cookie pair) is covered by the
		// live-boundary test below; here the browser jar is seeded with the
		// exact cookie attributes the backend mints (auth ADR §4), so this
		// test proves the client-side contract: only `mai_csrf` is ever read,
		// `mai_session` never leaves the HttpOnly jar, nothing touches storage.
		await page.context().addCookies([
			{
				name: 'mai_session',
				value: 's-fixturesessiontoken000000000000000000000',
				domain: '127.0.0.1',
				path: '/',
				httpOnly: true,
				sameSite: 'Lax'
			},
			{
				name: 'mai_csrf',
				value: 'c-fixturescsrftoken00000000000000000000000',
				domain: '127.0.0.1',
				path: '/',
				httpOnly: false,
				sameSite: 'Lax'
			}
		]);
		let bootstrapCsrf: string | null = null;
		let bootstrapSessionHeader: string | null = null;
		await mockApi(page, '/api/v1/auth/bootstrap', async (route) => {
			const headers = route.request().headers();
			bootstrapCsrf = headers['x-csrf-token'] ?? null;
			bootstrapSessionHeader = headers['cookie'] ?? null;
			await route.fulfill({ status: 201 });
		});

		await page.goto(FIXTURE_URL);
		const result = await page.evaluate(async () => {
			const probe = window.__maiApiClientProbe!;
			const client = probe.createMaiApiClient();
			await client.bootstrap({ password: 'fixture-password-not-real' });
			return {
				documentCookie: document.cookie,
				localStorageKeys: Object.keys(localStorage),
				sessionStorageKeys: Object.keys(sessionStorage)
			};
		});

		const cookies = await page.context().cookies();
		const session = cookies.find((cookie) => cookie.name === 'mai_session');
		expect(session?.httpOnly, 'session token must stay HttpOnly').toBe(true);
		expect(result.documentCookie).toContain('mai_csrf=c-fixturescsrftoken');
		expect(result.documentCookie).not.toContain('mai_session');
		expect(result.localStorageKeys).toEqual([]);
		expect(result.sessionStorageKeys).toEqual([]);
		// double-submit: the mutation header equals the readable cookie value
		expect(bootstrapCsrf).toBe('c-fixturescsrftoken00000000000000000000000');
		// the client relies on the cookie jar, it never carries the token itself
		expect(bootstrapSessionHeader).toContain('mai_session=s-fixturesessiontoken');
	});

	test('envelope errors carry typed code and fields through real fetch', async ({ page }) => {
		await mockApi(page, '/api/v1/servers/**', async (route) => {
			await route.fulfill({
				status: 409,
				contentType: 'application/json',
				body: JSON.stringify(ENVELOPE_CONFLICT)
			});
		});

		await page.goto(FIXTURE_URL);
		const rendered = await page.evaluate(async () => {
			const probe = window.__maiApiClientProbe!;
			const client = probe.createMaiApiClient({ getCsrfToken: () => 'c-t' });
			try {
				await client.editProfile('8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f', {
					name: 'x',
					prompt_revision: 1,
					environment: 'linux_ssh',
					ssh: {
						host: 'build.example.internal',
						port: 22,
						user: 'deploy',
						host_key: `SHA256:${'A'.repeat(43)}`,
						credential_ref: 'FIXTURE_SSH_KEY'
					},
					privilege: 'none',
					capabilities: [],
					expected_revision: 1
				});
				return { threw: false };
			} catch (error) {
				if (error instanceof probe.ApiError) {
					return { threw: true, status: error.status, code: error.code, requestId: error.requestId };
				}
				return { threw: true, status: -1, code: `unexpected:${String(error)}`, requestId: '' };
			}
		});

		expect(rendered).toEqual({ threw: true, status: 409, code: 'conflict', requestId: 'req-7f3a' });
	});

	test('event stream frames parse in the page; reconnect carries the last id', async ({ page }) => {
		const cursors: Array<string | undefined> = [];
		let responseIndex = 0;
		await mockApi(page, '/api/v1/events', async (route) => {
			cursors.push(route.request().headers()['last-event-id']);
			const body =
				responseIndex === 0
					? 'id: 43\ndata: {"seq":43,"run_id":"r1","ts":"2026-10-14T00:00:00Z","type":"step.completed","v":1,"data":{}}\n\n'
					: 'id: 44\ndata: {"seq":44,"run_id":"r1","ts":"2026-10-14T00:00:01Z","type":"step.started","v":1,"data":{}}\n\n';
			responseIndex += 1;
			await route.fulfill({
				status: 200,
				contentType: 'text/event-stream',
				body
			});
		});

		await page.goto(FIXTURE_URL);
		const seqs = await page.evaluate(async () => {
			const probe = window.__maiApiClientProbe!;
			const seqList: number[] = [];
			let cursor: string | null = null;
			for (let round = 0; round < 2; round += 1) {
				await probe.subscribeEvents('/api/v1/events', {
					cursor,
					onEvent: (frame) => {
						const event = probe.readMaiEvent(frame);
						if (event) {
							seqList.push(event.seq);
							cursor = frame.id;
						}
					}
				});
			}
			return seqList;
		});

		expect(seqs).toEqual([43, 44]);
		expect(cursors).toEqual([undefined, '43']);
	});
});

// ---------------------------------------------------------------------------
// Real boundary layer — live fixture backend (env-gated; see evidence README)
// ---------------------------------------------------------------------------

/** Synthetic fixture values; no real host, key or credential is exercised. */
const FIXTURE_SSH = {
	host: 'build.example.internal',
	port: 22,
	user: 'deploy',
	host_key: `SHA256:${'A'.repeat(43)}`,
	credential_ref: 'FIXTURE_SSH_KEY'
} as const;

const VALID_CREATE: components['schemas']['ProfileCreate'] = {
	name: 'fixture-alpha',
	prompt_revision: 1,
	environment: 'linux_ssh',
	ssh: FIXTURE_SSH,
	privilege: 'none',
	capabilities: []
};

/** Cookie-jar fetch shim: the browser jar this client normally relies on. */
function jarFetch(): {
	fetch: typeof fetch;
	getCsrfToken: () => string | null;
	clearSession: () => void;
} {
	const jar = new Map<string, string>();
	const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
		const headers = new Headers(init?.headers);
		const cookie = [...jar.entries()].map(([k, v]) => `${k}=${v}`).join('; ');
		if (cookie) headers.set('cookie', cookie);
		const response = await fetch(new Request(input, { ...init, headers }));
		for (const setCookie of response.headers.getSetCookie()) {
			const [pair] = setCookie.split(';');
			const eq = pair.indexOf('=');
			jar.set(pair.slice(0, eq), pair.slice(eq + 1));
		}
		return response;
	}) as typeof fetch;
	return {
		fetch: impl,
		getCsrfToken: () => jar.get('mai_csrf') ?? null,
		clearSession: () => {
			jar.delete('mai_session');
			jar.delete('mai_csrf');
		}
	};
}

test.describe('real MAI backend (MAI_E2E_API_URL)', () => {
	test.beforeAll(async () => {
		test.skip(
			!process.env.MAI_E2E_API_URL,
			'real-boundary run only: set MAI_E2E_API_URL to a live fixture backend'
		);
	});

	test('bootstrap → login → profile CRUD → conflict → logout, plus CSRF/origin denials', async () => {
		const baseUrl = process.env.MAI_E2E_API_URL!;
		const jar = jarFetch();
		// Browser-like Origin on mutations (the fixture allowlists this value).
		const client = createMaiApiClient({
			baseUrl,
			fetch: jar.fetch,
			getCsrfToken: jar.getCsrfToken,
			headers: { origin: 'http://127.0.0.1:4173' }
		});
		const password = `pw-${crypto.randomUUID()}`;

		expect(await client.health()).toEqual({ status: 'ok' });

		// G1: setup state. One-shot bootstrap; the second is a permanent 409.
		await client.bootstrap({ password });
		await expect(client.bootstrap({ password })).rejects.toMatchObject({
			status: 409,
			code: 'conflict'
		});

		// Wrong password is a value-free 401 (and starts the §5.3 penalty).
		await expect(client.login({ password: `${password}-wrong` })).rejects.toMatchObject({
			status: 401,
			code: 'unauthenticated'
		});
		await client.login({ password });

		// C02 surface through the typed boundary: create → read → edit → conflict.
		const created = await client.createProfile(VALID_CREATE);
		expect(created.revision).toBe(1);
		expect(created.schema_revision).toBe(1);

		const stored = await client.readProfile(created.server_id);
		expect(stored.name).toBe('fixture-alpha');
		expect(stored.projection).toEqual({ revision: 1, stale: false });

		await expect(
			client.refreshCsrf().then(() =>
				client.editProfile(created.server_id, {
					...VALID_CREATE,
					name: 'fixture-alpha-renamed',
					expected_revision: 1
				})
			)
		).resolves.toMatchObject({ server_id: created.server_id, revision: 2 });

		// Stale revision: structured, value-free conflict (C01 rule 6).
		await expect(
			client.editProfile(created.server_id, { ...VALID_CREATE, expected_revision: 1 })
		).rejects.toMatchObject({ status: 409, code: 'conflict' });

		// Negative controls at the real boundary: stripped CSRF / forged origin.
		const noCsrf = createMaiApiClient({
			baseUrl,
			fetch: jar.fetch,
			getCsrfToken: () => null,
			headers: { origin: 'http://127.0.0.1:4173' }
		});
		await expect(noCsrf.createProfile(VALID_CREATE)).rejects.toThrow(/no mai_csrf token/);
		const evilOrigin = createMaiApiClient({
			baseUrl,
			fetch: jar.fetch,
			getCsrfToken: jar.getCsrfToken,
			headers: { origin: 'https://evil.example' }
		});
		await expect(evilOrigin.createProfile(VALID_CREATE)).rejects.toMatchObject({
			status: 403,
			code: 'origin_rejected'
		});

		// Logout is effective on the next request (no grace window).
		await client.logout();
		jar.clearSession();
		await expect(client.readProfile(created.server_id)).rejects.toMatchObject({
			status: 401,
			code: 'unauthenticated'
		});
	});
});

test.describe('service-knowledge client surface (injected fetch)', () => {
	const SERVER_A = '5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60';
	const APP_MAIN = '0aaaaaaa-0000-4000-8000-000000000001';
	const FACT_ID = '11111111-2222-4333-8444-555555555555';
	const OPERATION_ID = '66666666-7777-4888-9999-aaaaaaaaaaaa';
	const KB = `/api/v1/servers/${SERVER_A}/applications/${APP_MAIN}/knowledge`;

	test('collection reads serialize the scoped route, paging, fact filter and status; GETs carry no CSRF header', async () => {
		const { fetch, calls } = stubFetch(() =>
			jsonResponse(200, { status: 'ok', found: false, items: [], total: 0 })
		);
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 'csrf-token-1' });

		await client.readKnowledge(SERVER_A, APP_MAIN, { limit: 5, offset: 10 });
		await client.readFactHistory(SERVER_A, APP_MAIN, FACT_ID, { limit: 3 });
		await client.readFactHistory(SERVER_A, APP_MAIN);
		await client.readKnowledgeOperations(SERVER_A, APP_MAIN, 'queued', { limit: 2, offset: 4 });

		expect(calls).toHaveLength(4);
		expect(calls[0].method).toBe('GET');
		expect(calls[0].url).toBe(`${STUB_ORIGIN}${KB}?limit=5&offset=10`);
		expect(calls[1].url).toBe(
			`${STUB_ORIGIN}${KB}/history?fact_id=${FACT_ID}&limit=3`
		);
		expect(calls[2].url).toBe(`${STUB_ORIGIN}${KB}/history`);
		expect(calls[3].url).toBe(
			`${STUB_ORIGIN}${KB}/operations?status=queued&limit=2&offset=4`
		);
		for (const call of calls) {
			expect(call.credentials).toBe('include');
			// Safe methods: the session cookie is the only credential layer.
			expect(call.headers['x-csrf-token']).toBeUndefined();
		}
	});

	test('retract posts the exact CAS body with the session CSRF transport', async () => {
		const { fetch, calls } = stubFetch(() =>
			jsonResponse(200, { status: 'applied', fact_id: FACT_ID, new_revision: 2 })
		);
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 'csrf-token-1' });

		const result = await client.retractKnowledgeFact(SERVER_A, APP_MAIN, FACT_ID, {
			expected_revision: 1,
			expected_generation: 1,
			reason: 'operator verified the statement is wrong'
		});

		expect(result.status).toBe('applied');
		const call = calls[0];
		expect(call.method).toBe('POST');
		expect(call.url).toBe(`${STUB_ORIGIN}${KB}/facts/${FACT_ID}/retract`);
		expect(call.headers['x-csrf-token']).toBe('csrf-token-1');
		expect(call.headers['content-type']).toBe('application/json');
		expect(JSON.parse(call.body ?? '')).toEqual({
			expected_revision: 1,
			expected_generation: 1,
			reason: 'operator verified the statement is wrong'
		});
	});

	test('retry posts with CSRF and surfaces the C01 conflict envelope as ApiError', async () => {
		const { fetch, calls } = stubFetch(() =>
			jsonResponse(
				409,
				{
					error: {
						code: 'operation_not_runnable',
						message: 'only unacknowledged queued operations are retryable here',
						request_id: 'req-1',
						fields: {}
					}
				}
			)
		);
		const client = createMaiApiClient({ fetch, getCsrfToken: () => 'csrf-token-1' });

		const error = (await client
			.retryKnowledgeOperation(SERVER_A, APP_MAIN, OPERATION_ID)
			.catch((thrown: unknown) => thrown)) as ApiError;

		expect(error).toBeInstanceOf(ApiError);
		expect(error.code).toBe('operation_not_runnable');
		expect(error.requestId).toBe('req-1');
		const call = calls[0];
		expect(call.method).toBe('POST');
		expect(call.url).toBe(
			`${STUB_ORIGIN}${KB}/operations/${OPERATION_ID}/retry`
		);
		expect(call.headers['x-csrf-token']).toBe('csrf-token-1');
	});
});
