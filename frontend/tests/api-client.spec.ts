import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import {
	ApiError,
	createMaiApiClient,
	readMaiEvent,
	subscribeEvents,
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
