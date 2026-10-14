import { expect, test, type Page, type Route } from '@playwright/test';
import { get } from 'svelte/store';
import { createMaiApiClient } from '../src/lib/api/client';
import { createSessionController } from '../src/lib/auth/session';
import type { SessionController, SessionSnapshot } from '../src/lib/auth/session';

/** Node globals (ambient: the project tsconfig targets browser-first code). */
declare const process: { env: Record<string, string | undefined> };

/**
 * 02-web-session — focused proofs for the login route and its scoped session
 * controller (frontend/src/lib/auth/session.ts over the reviewed api client).
 *
 * Three layers, one file (focused per test policy; the assembled login
 * journey stays owned by `02-verify`):
 *
 * 1. Controller semantics (Node, injected fetch): server-truth state machine
 *    around the authenticated CSRF re-mint probe, login/logout status and
 *    error mapping (frozen C01 codes: `unauthenticated`, `throttled`,
 *    `origin_rejected`), already-revoked logout idempotence, denied logout
 *    keeps the signed-in belief, instance isolation (no module state).
 * 2. Browser UI semantics (real Chromium against the real production SvelteKit
 *    build served by the playwright webServer, API route-mocked): the login
 *    route renders the login state from the real probe, renders appropriate
 *    session errors, empties web storage, and the password never persists.
 * 3. Live same-origin journey (env-gated, MAI_E2E_API_URL): the real browser
 *    talks to a REAL fixture backend (`uvicorn mai.api.app:create_app`) at the
 *    same origin — `/api/**` is relayed by a thin playwright proxy, exactly
 *    the production topology where the single TLS proxy routes `/api` to
 *    mai-api (contracts.md C08 rule 3). Covers the writer-level pieces of the
 *    gate scenario: login, wrong password, refresh, logout, revisit after
 *    logout (server truth, not a client flag), forged-cookie rejection;
 *    screenshots of every rendered outcome land in MAI_E2E_SHOT_DIR.
 */

// ---------------------------------------------------------------------------
// Layer 1 — controller semantics (injected fetch)
// ---------------------------------------------------------------------------

type FetchHandler = (request: Request) => Response | Promise<Response>;

/** Recording fetch stub: captures each exchange, delegates to the handler. */
function stubFetch(handler: FetchHandler): { fetch: typeof fetch; calls: Request[] } {
	const calls: Request[] = [];
	const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
		const resolved = input instanceof Request ? input : new Request(new URL(input.toString(), 'http://mai.test'), init);
		calls.push(resolved);
		return handler(resolved);
	}) as typeof fetch;
	return { fetch: impl, calls };
}

function envelope(status: number, code: string, message: string): Response {
	return new Response(
		JSON.stringify({ error: { code, message, request_id: 'req-fixture', fields: {} } }),
		{ status, headers: { 'content-type': 'application/json' } }
	);
}

// ---------------------------------------------------------------------------
// Layer 1 — controller semantics (injected fetch)
// ---------------------------------------------------------------------------

test.describe('session controller semantics (injected fetch)', () => {
	test('probe maps the authenticated CSRF re-mint: 204 in, 401 out, 5xx unavailable', async () => {
		let status: Response['status'] = 401;
		const { fetch } = stubFetch(() =>
			status === 204
				? new Response(null, { status: 204 })
				: envelope(status, status === 503 ? 'unavailable' : 'unauthenticated', 'no valid session')
		);
		const session = controller(fetch);
		expect(getSnapshot(session).status).toBe('checking');

		status = 401;
		await expect(session.probe()).resolves.toBe('unauthenticated');
		expect(getSnapshot(session)).toMatchObject({ status: 'unauthenticated', error: null });

		status = 204;
		await expect(session.probe()).resolves.toBe('authenticated');
		expect(getSnapshot(session).status).toBe('authenticated');

		status = 503;
		await expect(session.probe()).resolves.toBe('unavailable');
		expect(getSnapshot(session).error).toMatchObject({ code: 'unavailable' });
		expect(getSnapshot(session).status).toBe('unavailable');
	});

	test('wrong password surfaces the value-free stable code and stays unauthenticated', async () => {
		const { fetch } = stubFetch(() => envelope(401, 'unauthenticated', 'authentication failed'));
		const session = controller(fetch);

		await expect(session.login('not-the-password')).resolves.toBe('unauthenticated');
		expect(getSnapshot(session)).toMatchObject({
			status: 'unauthenticated',
			busy: false,
			error: { code: 'unauthenticated' }
		});
	});

	test('throttled login is its own error code (§5.3), distinct from wrong password', async () => {
		const { fetch } = stubFetch(() => envelope(429, 'throttled', 'too many failed login attempts'));
		const session = controller(fetch);

		await expect(session.login('anything')).resolves.toBe('unauthenticated');
		expect(getSnapshot(session).error).toMatchObject({ code: 'throttled' });
	});

	test('successful login authenticates, clears errors, and retains no credential', async () => {
		const seenSnapshots: SessionSnapshot[] = [];
		const { fetch, calls } = stubFetch(() => new Response(null, { status: 204 }));
		const session = controller(fetch);
		session.subscribe((snapshot) => seenSnapshots.push(snapshot));

		const password = 'fixture-secret-9f2a';
		await expect(session.login(password)).resolves.toBe('authenticated');
		expect(getSnapshot(session)).toMatchObject({ status: 'authenticated', error: null, busy: false });

		// The exchange is the plain frozen login route; the body was consumed.
		expect(calls[0].url).toContain('/api/v1/auth/login');
		// Nothing published on the store ever carries the submitted secret.
		for (const snapshot of seenSnapshots) {
			expect(JSON.stringify(snapshot)).not.toContain(password);
		}
	});

	test('logout: success and already-revoked both reach signed-out; a denial revokes nothing', async () => {
		let status: Response['status'] = 204;
		const { fetch } = stubFetch(() => (status === 204 ? new Response(null, { status: 204 }) : envelope(status, status === 401 ? 'unauthenticated' : 'csrf_rejected', 'denied')));
		const session = controller(fetch);

		// Arrange: signed in.
		status = 204;
		await session.login('pw');
		expect(getSnapshot(session).status).toBe('authenticated');

		// A boundary denial (origin/CSRF) did NOT revoke: keep signed-in + error.
		status = 403;
		await expect(session.logout()).resolves.toBe('authenticated');
		expect(getSnapshot(session)).toMatchObject({ status: 'authenticated', error: { code: 'csrf_rejected' } });

		// 401 on logout = the session was already gone; goal state reached.
		status = 401;
		await expect(session.logout()).resolves.toBe('unauthenticated');
		expect(getSnapshot(session)).toMatchObject({ status: 'unauthenticated', error: null });

		// Transport failure keeps the previous belief (a fresh instance is still
		// 'checking': nothing was revoked, nothing was learned) + the reason.
		const failing = controller((async () => {
			throw new TypeError('network down');
		}) as typeof fetch);
		await expect(failing.logout()).resolves.toBe('checking');
		expect(getSnapshot(failing)).toMatchObject({ status: 'checking', error: { code: 'unavailable' } });
	});

	test('controller instances share nothing (no module-level session state)', async () => {
		let authenticated = false;
		const { fetch } = stubFetch(() =>
			authenticated ? new Response(null, { status: 204 }) : envelope(401, 'unauthenticated', 'no valid session')
		);
		const first = controller(fetch);
		const second = controller(fetch);

		authenticated = true;
		await expect(first.probe()).resolves.toBe('authenticated');
		await expect(second.probe()).resolves.toBe('authenticated');

		authenticated = false;
		// A fresh instance re-asks the server; it never inherits a belief.
		const third = controller(fetch);
		await expect(third.probe()).resolves.toBe('unauthenticated');
	});
});

/**
 * One controller over the REAL reviewed client (`createMaiApiClient`) with an
 * injected fetch — the layer-1 proofs exercise session.ts + client.ts
 * together, not a hand-rolled double. The CSRF token source stands in for the
 * `mai_csrf` cookie the browser jar owns.
 */
function controller(doFetch: typeof fetch): SessionController {
	return createSessionController({
		client: createMaiApiClient({ fetch: doFetch, getCsrfToken: () => 'fixture-csrf-token' })
	});
}

function getSnapshot(controller: SessionController): SessionSnapshot {
	const snapshot = get(controller);
	expect(snapshot).toBeDefined();
	return snapshot;
}

// ---------------------------------------------------------------------------
// Layer 2 — login route in real Chromium (production build, API route-mocked)
// ---------------------------------------------------------------------------

const UNAUTHENTICATED_ENVELOPE = {
	error: { code: 'unauthenticated', message: 'authentication failed', request_id: 'req-ui', fields: {} }
} as const;
const THROTTLED_ENVELOPE = {
	error: { code: 'throttled', message: 'too many failed login attempts', request_id: 'req-ui-t', fields: {} }
} as const;

/** Registers the API mock; everything outside /api passes through to the app. */
async function mockApi(page: Page, path: string, handler: (route: Route) => Promise<void>) {
	await page.route(`**${path}`, handler);
}

test.describe('login route UI (production build, mocked API)', () => {
	test('unauthenticated arrival renders the form; storage stays empty; empty submit sends nothing', async ({
		page
	}) => {
		let probeCalls = 0;
		await mockApi(page, '/api/v1/auth/csrf', async (route) => {
			probeCalls += 1;
			await route.fulfill({
				status: 401,
				contentType: 'application/json',
				body: JSON.stringify(UNAUTHENTICATED_ENVELOPE)
			});
		});
		let loginCalls = 0;
		await mockApi(page, '/api/v1/auth/login', async (route) => {
			loginCalls += 1;
			await route.fulfill({
				status: 401,
				contentType: 'application/json',
				body: JSON.stringify(UNAUTHENTICATED_ENVELOPE)
			});
		});

		await page.goto('/login');
		const form = page.getByTestId('login-form');
		await expect(form).toBeVisible();

		// Client-side validation stops the empty submit before any byte.
		await page.getByTestId('login-submit').click();
		expect(loginCalls, 'empty password must not reach the API').toBe(0);
		await expect(page.locator('.mai-field__error')).toContainText('Enter the administrator password');

		// Wrong password: the stable code renders as the alert.
		await page.locator('input[type="password"]').fill('wrong-password');
		await page.getByTestId('login-submit').click();
		const alert = page.getByTestId('login-api-error');
		await expect(alert).toBeVisible();
		await expect(alert).toHaveAttribute('data-error-code', 'unauthenticated');
		// The submitted secret is gone from the DOM after the attempt.
		await expect(page.locator('input[type="password"]')).toHaveValue('');

		expect(probeCalls, 'exactly one arrival probe').toBe(1);
		// No credential or session state is ever persisted client-side.
		const storage = await page.evaluate(() => ({
			local: Object.keys(localStorage),
			session: Object.keys(sessionStorage),
			cookie: document.cookie
		}));
		expect(storage.local).toEqual([]);
		expect(storage.session).toEqual([]);
		expect(storage.cookie).not.toContain('mai_session');
	});

	test('each session error code renders distinctly (wrong password vs throttled)', async ({ page }) => {
		let status = 401;
		let body: unknown = UNAUTHENTICATED_ENVELOPE;
		await mockApi(page, '/api/v1/auth/login', async (route) => {
			await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
		});
		await mockApi(page, '/api/v1/auth/csrf', async (route) => {
			await route.fulfill({
				status: 401,
				contentType: 'application/json',
				body: JSON.stringify(UNAUTHENTICATED_ENVELOPE)
			});
		});

		await page.goto('/login');
		await expect(page.getByTestId('login-form')).toBeVisible();

		const submit = async (password: string) => {
			await page.locator('input[type="password"]').fill(password);
			await page.getByTestId('login-submit').click();
		};

		await submit('nope');
		await expect(page.getByTestId('login-api-error')).toHaveAttribute(
			'data-error-code',
			'unauthenticated'
		);

		status = 429;
		body = THROTTLED_ENVELOPE;
		await submit('nope');
		await expect(page.getByTestId('login-api-error')).toHaveAttribute('data-error-code', 'throttled');
	});

	test('successful login navigates to the workspace; signed-in /login card offers sign-out', async ({
		page
	}) => {
		let authenticated = false;
		await mockApi(page, '/api/v1/auth/csrf', async (route) => {
			await route.fulfill({
				status: authenticated ? 204 : 401,
				contentType: 'application/json',
				body: JSON.stringify(UNAUTHENTICATED_ENVELOPE)
			});
		});
		await mockApi(page, '/api/v1/auth/login', async (route) => {
			authenticated = true;
			await route.fulfill({ status: 204 });
		});
		await mockApi(page, '/api/v1/auth/logout', async (route) => {
			authenticated = false;
			await route.fulfill({ status: 204 });
		});
		// The mock cannot mint Set-Cookie pairs, so the jar is seeded with the
		// exact cookie attributes the backend mints at login (auth ADR §4) —
		// logout is a session-bearing mutation and must fail closed without a
		// readable `mai_csrf` (reviewed client guard, proven by this seeding).
		await page.context().addCookies([
			{
				name: 'mai_session',
				value: 's-mocksessiontoken000000000000000000000000',
				domain: '127.0.0.1',
				path: '/',
				httpOnly: true,
				sameSite: 'Lax'
			},
			{
				name: 'mai_csrf',
				value: 'c-mockcsrftoken0000000000000000000000000',
				domain: '127.0.0.1',
				path: '/',
				httpOnly: false,
				sameSite: 'Lax'
			}
		]);

		await page.goto('/login');
		await expect(page.getByTestId('login-form')).toBeVisible();
		await page.locator('input[type="password"]').fill('fixture-password');
		await page.getByTestId('login-submit').click();

		// Route-owned deep-link adapter: the desktop workspace at `/`.
		await expect(page).toHaveURL(/\/$/);
		await expect(page.getByTestId('component-host')).toBeVisible();

		// Revisiting the login route shows the signed-in card (real probe answer).
		await page.goto('/login');
		await expect(page.getByTestId('login-signed-in')).toBeVisible();

		// Sign out returns the form.
		await page.getByTestId('login-sign-out').click();
		await expect(page.getByTestId('login-form')).toBeVisible();

		const storage = await page.evaluate((password) => {
			const frameworkKeys = ['sveltekit:snapshot', 'sveltekit:scroll'];
			return {
				local: Object.keys(localStorage),
				sessionExtra: Object.keys(sessionStorage).filter((key) => !frameworkKeys.includes(key)),
				credentialLeak: [document.cookie, JSON.stringify(localStorage), JSON.stringify(sessionStorage)]
					.join('\n')
					.includes(password)
			};
		}, 'fixture-password');
		// The credential criterion: nothing in web storage, ever. SvelteKit's
		// own navigation bookkeeping (snapshot/scroll) is framework-owned and
		// holds no session or credential material — asserted by exclusion.
		expect(storage.local).toEqual([]);
		expect(storage.sessionExtra).toEqual([]);
		expect(storage.credentialLeak).toBe(false);
	});

	test('unreachable boundary renders the unavailable state instead of the form alone', async ({
		page
	}) => {
		await mockApi(page, '/api/v1/auth/csrf', async (route) => {
			await route.fulfill({
				status: 503,
				contentType: 'application/json',
				body: JSON.stringify({
					error: { code: 'unavailable', message: 'database unhealthy', request_id: 'req-ui-x', fields: {} }
				})
			});
		});

		await page.goto('/login');
		await expect(page.getByTestId('login-unavailable')).toBeVisible();
	});
});

// ---------------------------------------------------------------------------
// Layer 3 — live same-origin journey (env-gated real backend)
// ---------------------------------------------------------------------------

/** Relays same-origin `/api/**` to the real fixture backend (no mocking). */
async function proxyApi(page: Page, backendUrl: string) {
	await page.route('**/api/**', async (route) => {
		const request = route.request();
		const target = new URL(request.url());
		const url = backendUrl + target.pathname + target.search;
		const response = await route.fetch({ url });
		await route.fulfill({ response });
	});
}

const SHOT_DIR = process.env.MAI_E2E_SHOT_DIR ?? '/tmp/mai-fxt-02-web-session/shots';

async function shot(page: Page, name: string): Promise<void> {
	await page.screenshot({ path: `${SHOT_DIR}/${name}`, fullPage: true });
}

test.describe('live same-origin journey (MAI_E2E_API_URL)', () => {
	test.describe.configure({ mode: 'serial' });
	test.beforeAll(async () => {
		test.skip(
			!process.env.MAI_E2E_API_URL,
			'real-boundary run only: set MAI_E2E_API_URL to a live fixture backend'
		);
	});

	test('login → wrong password → refresh → logout → revisit, all through the real API', async ({
		page
	}) => {
		const backend = process.env.MAI_E2E_API_URL!;
		await proxyApi(page, backend);

		// -- Unauthenticated arrival: the real boundary says 401 (setup state).
		await page.goto('/login');
		await expect(page.getByTestId('login-form')).toBeVisible();
		await shot(page, '01-login-unauthenticated.png');

		// -- Fixture setup at the real boundary: one-shot bootstrap with the
		//    explicitly-sent origin non-browser clients must use (auth ADR §5).
		//    The synthetic credential exists only inside this process/fixture.
		const password = `pw-${crypto.randomUUID()}`;
		const bootstrapUrl = `${backend}/api/v1/auth/bootstrap`;
		const bootstrapped = await fetch(bootstrapUrl, {
			method: 'POST',
			headers: { 'content-type': 'application/json', origin: 'http://127.0.0.1:4173' },
			body: JSON.stringify({ password })
		});
		expect(bootstrapped.status, 'one-shot bootstrap creates the credential').toBe(201);
		// Negative control: the setup endpoint is permanently closed afterwards.
		const rebootstrap = await fetch(bootstrapUrl, {
			method: 'POST',
			headers: { 'content-type': 'application/json', origin: 'http://127.0.0.1:4173' },
			body: JSON.stringify({ password })
		});
		expect(rebootstrap.status).toBe(409);
		expect(await rebootstrap.json()).toMatchObject({ error: { code: 'conflict' } });

		// -- Wrong password: a value-free 401 envelope from the real verifier.
		await page.locator('input[type="password"]').fill(`definitely-not-${crypto.randomUUID()}`);
		await page.getByTestId('login-submit').click();
		const alert = page.getByTestId('login-api-error');
		await expect(alert).toBeVisible();
		await expect(alert).toHaveAttribute('data-error-code', 'unauthenticated');
		await shot(page, '02-wrong-password-error.png');

		// -- Real login: fresh token pair minted by the backend at the same origin.
		await page.locator('input[type="password"]').fill(password);
		await page.getByTestId('login-submit').click();
		await expect(page).toHaveURL(/\/$/);
		await expect(page.getByTestId('component-host')).toBeVisible();
		await shot(page, '03-logged-in-workspace.png');

		const cookies = await page.context().cookies();
		const session = cookies.find((cookie) => cookie.name === 'mai_session');
		const csrf = cookies.find((cookie) => cookie.name === 'mai_csrf');
		expect(session, 'session cookie minted at the same origin').toBeTruthy();
		expect(session!.httpOnly).toBe(true);
		expect(session!.sameSite).toBe('Lax');
		expect(csrf, 'CSRF cookie readable by first-party JS').toBeTruthy();
		const storage = await page.evaluate((secret) => {
			const frameworkKeys = ['sveltekit:snapshot', 'sveltekit:scroll'];
			return {
				local: Object.keys(localStorage),
				sessionExtra: Object.keys(sessionStorage).filter((key) => !frameworkKeys.includes(key)),
				cookie: document.cookie,
				credentialLeak: [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]
					.join('\n')
					.includes(secret)
			};
		}, password);
		expect(storage.local).toEqual([]);
		expect(storage.sessionExtra).toEqual([]);
		expect(storage.cookie).not.toContain('mai_session');
		expect(storage.credentialLeak).toBe(false);

		// -- Refresh: the page reloads; the HttpOnly jar still authenticates.
		await page.reload();
		await expect(page.getByTestId('component-host')).toBeVisible();

		// -- Revisit the login route: the real probe says authenticated.
		await page.goto('/login');
		await expect(page.getByTestId('login-signed-in')).toBeVisible();
		await shot(page, '04-revisit-signed-in.png');

		// -- Logout through the real boundary (origin + CSRF layers on top).
		await page.getByTestId('login-sign-out').click();
		await expect(page.getByTestId('login-form')).toBeVisible();
		await shot(page, '05-after-logout-form.png');
		const afterLogout = await page.context().cookies();
		expect(afterLogout.find((cookie) => cookie.name === 'mai_session')).toBeUndefined();

		// Revocation is effective on the next request — no grace window (S2).
		const probe = await page.evaluate(async () => {
			const response = await fetch('/api/v1/auth/csrf', { credentials: 'include' });
			return { status: response.status, body: await response.json().catch(() => null) };
		});
		expect(probe.status).toBe(401);
		expect(probe.body).toMatchObject({ error: { code: 'unauthenticated' } });

		// -- Revisit after logout: the login state again, from server truth.
		await page.goto('/login');
		await expect(page.getByTestId('login-form')).toBeVisible();

		// -- Negative control: a forged session cookie is not authentication.
		//    The client-side jar means nothing; the server rejects the token.
		await page.context().addCookies([
			{
				name: 'mai_session',
				value: 'forged-token-not-minted-by-anyone',
				domain: '127.0.0.1',
				path: '/',
				httpOnly: true,
				sameSite: 'Lax'
			}
		]);
		await page.goto('/login');
		await expect(page.getByTestId('login-form')).toBeVisible();
		await shot(page, '06-forged-cookie-still-login.png');
	});
});
