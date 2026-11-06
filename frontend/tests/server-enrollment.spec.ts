import { expect, test, type Page, type Route } from '@playwright/test';

/**
 * so-03-enrollment-ui — real-Chromium proofs for the guided enrollment flow
 * (frontend/src/lib/workspace/ServerEnrollment.svelte) composed into the REAL
 * DesktopShell on the REAL app route. The production build served by the
 * Playwright webServer is the exercised entrypoint.
 *
 * MOCK HONESTY (so_PLAN scenario 2): the API is mocked at the HTTP boundary
 * (browser route interception) and every interaction flows through the REAL
 * typed client (`MaiApiClient.readControllerKey` / `testOnboardingConnection`
 * / `enrollViaOnboarding`) and the real Svelte composition. NOTHING here is
 * an SSH integration proof — the real-transport OpenSSH behavior is owned by
 * backend so-01/so-02 tests and the so-04 full-stack verification.
 *
 * Coverage:
 *  - controller public key: loading → ready, Copy writes EXACTLY the public
 *    key (Chromium clipboard permission + read-back), execCommand fallback,
 *    failure state with recovery;
 *  - no `Credential reference` / `Host key (SHA256 fingerprint)` input and no
 *    literal `MAI_SSH_KEY_MAI` anywhere in the UI;
 *  - ONE guided Test action: discovery with a single candidate continues into
 *    the pinned test in the SAME click; several candidates require explicit
 *    selection (discovery alone is never presented as authentication);
 *  - the pinned result (fingerprint + Linux identity + privilege) enables the
 *    trust checkbox and Enroll; any host edit invalidates result + trust;
 *  - enroll payload: `host_key_confirmed: true`, tested fingerprint, NO
 *    `credential_ref`; CSRF double-submit header; success closes the drawer,
 *    refreshes and selects the new server; failure preserves the form;
 *  - duplicate in-flight submissions are disabled;
 *  - honest states at 1280x800 and 360x640 without horizontal overflow.
 */

const CSRF_COOKIE = 'fixture-csrf-token';
const CONTROLLER_KEY = {
	public_key:
		'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIF1xTUr3FIXTUREPUBL1CKEY00000000000000 mai-controller',
	key_type: 'ssh-ed25519',
	fingerprint: 'SHA256:ExAmPlEF1xTuReF1nGeRPr1nT0000000000000000000'
} as const;

const FP_A = 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
const FP_B = 'SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB';

const SERVER_ALPHA = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const NEW_SERVER = 'c0ffee00-1234-4abc-9def-0123456789ab';

function profileFixture(serverId: string, name: string) {
	return {
		schema_version: 1,
		server_id: serverId,
		name,
		prompt_revision: 1,
		prompt_text: 'fixture prompt',
		environment: 'linux_ssh',
		ssh: {
			host: `${name}.example.internal`,
			port: 22,
			user: 'deploy',
			host_key: `SHA256:${'f'.repeat(43)}`,
			credential_ref: 'MAI_SSH_KEY'
		},
		privilege: 'sudo',
		capabilities: ['host.shell'],
		revision: 1,
		sha256: 'a'.repeat(64),
		projection: { revision: 1, stale: false }
	};
}

function pinnedResult(hostKey: string) {
	return {
		host_key: hostKey,
		key_type: 'ssh-ed25519',
		authentication: 'ok',
		privilege: 'sudo',
		probe_environment: {
			hostname: 'fixture-target',
			os_id: 'arch',
			os_version_id: 'rolling',
			kernel: '6.12.0-fixture',
			boot_id: 'fixture-boot-id'
		}
	};
}

function json(route: Route, status: number, body: unknown): Promise<void> {
	return route.fulfill({
		status,
		contentType: 'application/json',
		body: JSON.stringify(body)
	});
}

interface TestHarness {
	/** GET /api/v1/servers call count (fresh-list refreshes after enroll). */
	listCalls: number;
	/** Bodies of every /onboarding/test request, in arrival order. */
	testCalls: Array<Record<string, unknown>>;
	/** Bodies + observed CSRF header of every /onboarding/enroll request. */
	enrollCalls: Array<{ body: Record<string, unknown>; csrf: string | null }>;
	candidates: Array<{ host_key: string; key_type: string }>;
	pinned: Record<string, unknown> | null;
	enrollStatus: number;
	enrollBody: unknown;
	testDelayMs: 0 | number;
	setCandidates(candidates: Array<{ host_key: string; key_type: string }>): void;
	setPinnedResult(result: Record<string, unknown> | null): void;
	setEnrollAnswer(status: number, body: unknown): void;
	setTestDelay(ms: number): void;
}

function createHarness(): TestHarness {
	return {
		listCalls: 0,
		testCalls: [] as Array<Record<string, unknown>>,
		enrollCalls: [] as Array<{ body: Record<string, unknown>; csrf: string | null }>,
		candidates: [{ host_key: FP_A, key_type: 'ssh-ed25519' }],
		pinned: pinnedResult(FP_A) as Record<string, unknown> | null,
		enrollStatus: 201,
		enrollBody: {
			server_id: NEW_SERVER,
			revision: 1,
			schema_revision: 1,
			sha256: 'b'.repeat(64)
		} as unknown,
		testDelayMs: 0,
		setCandidates(candidates: Array<{ host_key: string; key_type: string }>) {
			this.candidates = candidates;
		},
		setPinnedResult(result: Record<string, unknown> | null) {
			this.pinned = result;
		},
		setEnrollAnswer(status: number, body: unknown) {
			this.enrollStatus = status;
			this.enrollBody = body;
		},
		setTestDelay(ms: number) {
			this.testDelayMs = ms;
		}
	};
}

/** Wires the shared live-desktop + onboarding mocks; returns the recorder. */
async function armMocks(page: Page): Promise<TestHarness> {
	await page.context().addCookies([
		{ name: 'mai_csrf', value: CSRF_COOKIE, url: 'http://127.0.0.1:4173' }
	]);

	const harness = createHarness();

	await page.route('**/api/v1/servers', (route) => {
		harness.listCalls += 1;
		const servers = [profileFixture(SERVER_ALPHA, 'Server Alpha')];
		if (harness.enrollCalls.length > 0) {
			servers.push(profileFixture(NEW_SERVER, 'Fixture Target'));
		}
		return json(route, 200, { servers });
	});
	// Live-attach companions observed by the desktop-shell live-nav proof.
	// D09: the live shell's drawer rides the agent-session history; the
// harness serves one honest empty bounded page so the post-enroll refresh
// succeeds instead of surfacing a mock-gap 404 over the enrollment status.
await page.route('**/api/v1/agent-sessions?**', (route) => {
	const url = new URL(route.request().url());
	return route.fulfill({
		status: 200,
		contentType: 'application/json',
		body: JSON.stringify({
			limit: Number(url.searchParams.get('limit') ?? 10),
			offset: Number(url.searchParams.get('offset') ?? 0),
			total: 0,
			has_more: false,
			items: []
		})
	});
});
await page.route('**/api/v1/servers/*/memory/status', (route) =>
		json(route, 200, {
			status: 'ok',
			code: null,
			provider: 'hindsight',
			operation: 'status',
			api_version: 'v1.2.3',
			error_type: null
		})
	);
	await page.route('**/api/v1/applications/catalog**', (route) =>
		json(route, 200, { runbooks: [], categories: [], limit: 10, offset: 0, total: 0 })
	);
	await page.route('**/api/v1/models/profiles', (route) =>
		json(route, 200, {
			profiles: [
				{
					schema_version: 1,
					name: 'glm53flash',
					connection: 'litellm',
					model: 'z-ai-coding/glm-5.3-flash'
				}
			]
		})
	);
	await page.route('**/api/v1/models/selection', (route) =>
		json(route, 200, { schema_version: 1, connection: 'litellm', profile: 'glm53flash' })
	);

	await page.route('**/api/v1/servers/onboarding/controller-key', (route) =>
		json(route, 200, CONTROLLER_KEY)
	);

	await page.route('**/api/v1/servers/onboarding/test', async (route) => {
		if (harness.testDelayMs > 0) {
			const { promise, resolve } = Promise.withResolvers<void>();
			setTimeout(resolve, harness.testDelayMs);
			await promise;
		}
		const body = route.request().postDataJSON() as Record<string, unknown>;
		harness.testCalls.push(body);
		if (body.host_key === null) {
			return json(route, 200, { candidates: harness.candidates });
		}
		if (harness.pinned !== null && body.host_key === harness.pinned.host_key) {
			return json(route, 200, harness.pinned);
		}
		return json(route, 409, {
			error: {
				code: 'conflict',
				message: 'target presents none of the accepted fingerprints',
				request_id: 'req-so03',
				fields: {}
			}
		});
	});

	await page.route('**/api/v1/servers/onboarding/enroll', async (route) => {
		harness.enrollCalls.push({
			body: route.request().postDataJSON() as Record<string, unknown>,
			csrf: route.request().headers()['x-csrf-token'] ?? null
		});
		return json(route, harness.enrollStatus, harness.enrollBody);
	});

	return harness;
}

/** Opens the live desktop, the enrollment drawer, and waits for the key. */
async function openEnrollment(page: Page): Promise<void> {
	await page.goto('/');
	await expect(page.getByTestId('desktop-enroll-toggle')).toBeVisible();
	await page.getByTestId('desktop-enroll-toggle').click();
	await expect(page.getByTestId('enroll-controller-public-key')).toBeVisible();
}

/** Fills the SSH endpoint fields (invalidates any prior test state). */
async function fillTarget(page: Page): Promise<void> {
	await page.getByLabel('SSH host').fill('target.example.internal');
	await page.getByLabel('SSH port').fill('2202');
	await page.getByLabel('SSH user').fill('deploy');
	await page.getByLabel('Privilege mode').selectOption('sudo');
}

/** The single trust checkbox inside the enrollment drawer. */
function trustCheckbox(page: Page) {
	return page.getByTestId('enroll-host-key-confirmation').getByRole('checkbox');
}

test.describe('guided enrollment (so-03)', () => {
	test.use({ viewport: { width: 1280, height: 800 } });

	test.beforeEach(async ({ page }) => {
		await armMocks(page);
	});

	test('loads the controller public key, copies EXACTLY it, and shows no credential/fingerprint input', async ({
		page
	}) => {
		await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
		await page.goto('/');
		await page.getByTestId('desktop-enroll-toggle').click();

		// Honest ready state after the bounded load.
		const key = page.getByTestId('enroll-controller-public-key');
		await expect(key).toHaveText(CONTROLLER_KEY.public_key);
		await expect(page.getByTestId('enroll-copy-status')).toHaveText('');

		// No legacy inputs and no hardcoded default credential anywhere.
		await expect(page.getByText('Credential reference')).toHaveCount(0);
		await expect(page.getByText('Host key (SHA256 fingerprint)')).toHaveCount(0);
		const dom = await page.content();
		expect(dom).not.toContain('MAI_SSH_KEY_MAI');
		expect(dom).not.toContain('PRIVATE KEY');

		// Keyboard: focus + Enter (native button activation) copies the key.
		await page.getByTestId('enroll-copy-public-key').focus();
		await page.keyboard.press('Enter');
		await expect(page.getByTestId('enroll-copy-status')).toHaveText('Copied.');
		const clipboard = await page.evaluate(() => navigator.clipboard.readText());
		expect(clipboard).toBe(CONTROLLER_KEY.public_key);
	});

	test('copy falls back to the supported execCommand path when the Clipboard API is unavailable', async ({
		page
	}) => {
		await page.addInitScript(() => {
			Object.defineProperty(navigator, 'clipboard', {
				get: () => undefined,
				configurable: true
			});
		});
		await openEnrollment(page);

		await page.getByTestId('enroll-copy-public-key').click();
		await expect(page.getByTestId('enroll-copy-status')).toHaveText('Copied.');
	});

	test('controller-key failure is honest and Retry recovers', async ({ page }) => {
		await page.route('**/api/v1/servers/onboarding/controller-key', (route) =>
			json(route, 503, {
				error: {
					code: 'unavailable',
					message: 'credential resolver unavailable',
					request_id: 'req-key-503',
					fields: {}
				}
			})
		);
		await page.goto('/');
		await page.getByTestId('desktop-enroll-toggle').click();

		const errorBox = page.getByTestId('enroll-key-error');
		await expect(errorBox).toContainText('unavailable');
		await expect(page.getByTestId('enroll-controller-public-key')).toHaveCount(0);

		// Recover: the last matching route wins — answer 200 now.
		await page.route('**/api/v1/servers/onboarding/controller-key', (route) =>
			json(route, 200, CONTROLLER_KEY)
		);
		await page.getByTestId('enroll-key-retry').click();
		await expect(page.getByTestId('enroll-controller-public-key')).toHaveText(
			CONTROLLER_KEY.public_key
		);
	});

	test('one Test click on a single candidate discovers AND pinned-tests in the same action', async ({
		page
	}) => {
		const harness = await armMocks(page);
		await openEnrollment(page);
		await fillTarget(page);

		// Trust + Enroll are gated BEFORE any test ran.
		await expect(trustCheckbox(page)).toBeDisabled();
		await expect(page.getByTestId('enroll-submit')).toBeDisabled();

		await page.getByTestId('enroll-test-connection').click();

		// The ONE action produced discovery + pinned test back-to-back.
		await expect(page.getByTestId('enroll-test-result')).toBeVisible();
		expect(harness.testCalls).toHaveLength(2);
		expect(harness.testCalls[0]).toMatchObject({
			ssh: { host: 'target.example.internal', port: 2202, user: 'deploy' },
			privilege: 'sudo',
			host_key: null,
			timeout_seconds: 10
		});
		expect(harness.testCalls[1]).toMatchObject({
			host_key: FP_A,
			timeout_seconds: 10
		});

		// Only OnboardingTestResult may claim authentication success.
		const result = page.getByTestId('enroll-test-result');
		await expect(result).toContainText(FP_A);
		await expect(result).toContainText('Authenticated');
		await expect(result).toContainText('fixture-target');
		await expect(result).toContainText('arch');
		await expect(page.getByTestId('enroll-test-notice')).toHaveCount(0);
		await expect(page.getByTestId('enroll-host-key-candidates')).toHaveCount(0);

		// Enroll stays gated on explicit trust even after success — and on a
		// complete form: trust alone is not enough while name/prompt are empty.
		await expect(trustCheckbox(page)).toBeEnabled();
		await expect(trustCheckbox(page)).not.toBeChecked();
		await expect(page.getByTestId('enroll-submit')).toBeDisabled();
		await trustCheckbox(page).check();
		await expect(page.getByTestId('enroll-submit')).toBeDisabled();
		await page.getByLabel('Display name').fill('Fixture Target');
		await page.getByTestId('enroll-prompt-text').fill('# Fixture prompt');
		await expect(page.getByTestId('enroll-submit')).toBeEnabled();
	});

	test('multiple candidates require selection; selection arms the pinned test; edits invalidate', async ({
		page
	}) => {
		const harness = await armMocks(page);
		harness.setCandidates([
			{ host_key: FP_A, key_type: 'ssh-ed25519' },
			{ host_key: FP_B, key_type: 'rsa-sha2-512' }
		]);
		await openEnrollment(page);
		await fillTarget(page);

		await page.getByTestId('enroll-test-connection').click();

		// Discovery alone: candidates rendered, NO authentication claim.
		const candidates = page.getByTestId('enroll-host-key-candidates');
		await expect(candidates).toBeVisible();
		await expect(candidates).toContainText(FP_A);
		await expect(candidates).toContainText(FP_B);
		await expect(page.getByTestId('enroll-test-notice')).toContainText(
			'NOT yet an authenticated'
		);
		await expect(page.getByTestId('enroll-test-result')).toHaveCount(0);
		await expect(trustCheckbox(page)).toBeDisabled();
		expect(harness.testCalls).toHaveLength(1);

		// Selection arms the pinned test; the button states it plainly.
		await page
			.getByTestId('enroll-host-key-candidates')
			.getByRole('radio')
			.nth(1)
			.check();
		await expect(page.getByTestId('enroll-test-connection')).toHaveText(
			'Test selected host key'
		);
		await expect(page.getByTestId('enroll-test-result')).toHaveCount(0);

		// Second click = actual pinned authentication test.
		harness.setPinnedResult(pinnedResult(FP_B));
		await page.getByTestId('enroll-test-connection').click();
		const result = page.getByTestId('enroll-test-result');
		await expect(result).toBeVisible();
		await expect(result).toContainText(FP_B);
		expect(harness.testCalls).toHaveLength(2);
		expect(harness.testCalls[1]).toMatchObject({ host_key: FP_B });

		// Trust becomes available only now; enroll needs the completed form too.
		await expect(trustCheckbox(page)).toBeEnabled();
		await trustCheckbox(page).check();
		await expect(page.getByTestId('enroll-submit')).toBeDisabled();
		await page.getByLabel('Display name').fill('Fixture Target');
		await page.getByTestId('enroll-prompt-text').fill('# Fixture prompt');
		await expect(page.getByTestId('enroll-submit')).toBeEnabled();

		// ANY endpoint edit invalidates result + trust — no stale enrollment.
		await page.getByLabel('SSH host').fill('moved.example.internal');
		await expect(page.getByTestId('enroll-test-result')).toHaveCount(0);
		await expect(page.getByTestId('enroll-host-key-candidates')).toHaveCount(0);
		await expect(trustCheckbox(page)).not.toBeChecked();
		await expect(trustCheckbox(page)).toBeDisabled();
		await expect(page.getByTestId('enroll-submit')).toBeDisabled();
		await expect(page.getByTestId('enroll-test-connection')).toHaveText('Test connection');
	});

	test('enroll submits the confirmed payload without credential_ref, then closes, refreshes and selects', async ({
		page
	}) => {
		const harness = await armMocks(page);
		await openEnrollment(page);
		await fillTarget(page);
		await page.getByLabel('Display name').fill('Fixture Target');
		await page
			.getByTestId('enroll-prompt-text')
			.fill('# Fixture runbook\nKeep this target disposable.');

		await page.getByTestId('enroll-test-connection').click();
		await expect(page.getByTestId('enroll-test-result')).toBeVisible();
		await trustCheckbox(page).check();

		await expect(page.getByTestId('enroll-submit')).toBeEnabled();
		await page.getByTestId('enroll-submit').click();

		// Exact payload: confirmed trust, tested fingerprint, NO credential_ref.
		expect(harness.enrollCalls).toHaveLength(1);
		const { body, csrf } = harness.enrollCalls[0];
		expect(csrf).toBe(CSRF_COOKIE);
		expect(body).toMatchObject({
			name: 'Fixture Target',
			prompt_revision: 1,
			prompt_text: '# Fixture runbook\nKeep this target disposable.',
			environment: 'linux_ssh',
			privilege: 'sudo',
			ssh: { host: 'target.example.internal', port: 2202, user: 'deploy' },
			host_key: FP_A,
			host_key_confirmed: true
		});
		expect(body).not.toHaveProperty('credential_ref');
		expect(JSON.stringify(body)).not.toContain('MAI_SSH_KEY_MAI');

		// Shell hand-off: drawer closed, servers re-fetched, new server selected.
		await expect(page.getByTestId('enroll-server')).toHaveCount(0);
		await expect(page.getByTestId('desktop-enroll-toggle')).toHaveText('Enroll server');
		// The transient status line races the drawer's session-history
		// refresh (both are honest writers); the FUNCTIONAL claims are the
		// enrollment call count and the selected server.
		expect(harness.listCalls).toBeGreaterThanOrEqual(2);
		await expect(
			page.getByTestId('server-select').locator('option:checked')
		).toHaveText('Active: Fixture Target');

		// No horizontal overflow at 1280x800.
		expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
			1280
		);
	});

	test('enroll failure preserves the form and shows the error honestly', async ({ page }) => {
		const harness = await armMocks(page);
		harness.setEnrollAnswer(409, {
			error: {
				code: 'conflict',
				message: 'target presents none of the accepted fingerprints',
				request_id: 'req-enroll-409',
				fields: {}
			}
		});
		await openEnrollment(page);
		await fillTarget(page);
		await page.getByLabel('Display name').fill('Fixture Target');
		await page.getByTestId('enroll-prompt-text').fill('# Fixture prompt');

		await page.getByTestId('enroll-test-connection').click();
		await expect(page.getByTestId('enroll-test-result')).toBeVisible();
		await trustCheckbox(page).check();
		await page.getByTestId('enroll-submit').click();

		await expect(page.getByTestId('enroll-error')).toContainText('conflict');

		// Form preserved: no fabricated success, inputs kept, drawer open.
		await expect(page.getByTestId('enroll-server')).toBeVisible();
		await expect(page.getByLabel('Display name')).toHaveValue('Fixture Target');
		await expect(page.getByLabel('SSH host')).toHaveValue('target.example.internal');
		await expect(page.getByTestId('enroll-test-result')).toBeVisible();
		await expect(page.getByTestId('desktop-status')).not.toContainText('Server enrolled');
	});

	test('in-flight test and enroll submissions cannot be duplicated', async ({ page }) => {
		const harness = await armMocks(page);
		harness.setTestDelay(400);
		await openEnrollment(page);
		await fillTarget(page);

		const testButton = page.getByTestId('enroll-test-connection');
		await testButton.click();
		await expect(testButton).toBeDisabled();
		// A synthetic second activation on the disabled control must no-op.
		await testButton.dispatchEvent('click');
		await expect(page.getByTestId('enroll-test-result')).toBeVisible({ timeout: 5_000 });
		expect(harness.testCalls).toHaveLength(2); // discovery + auto pinned, ONE action

		// Enroll is guarded while its request is in flight (delayed answer).
		await page.route('**/api/v1/servers/onboarding/enroll', async (route) => {
			{
				const { promise, resolve } = Promise.withResolvers<void>();
				setTimeout(resolve, 300);
				await promise;
			}
			harness.enrollCalls.push({
				body: route.request().postDataJSON() as Record<string, unknown>,
				csrf: route.request().headers()['x-csrf-token'] ?? null
			});
			return json(route, 201, {
				server_id: NEW_SERVER,
				revision: 1,
				schema_revision: 1,
				sha256: 'b'.repeat(64)
			});
		});
		await page.getByLabel('Display name').fill('Fixture Target');
		await page.getByTestId('enroll-prompt-text').fill('# Fixture prompt');
		await trustCheckbox(page).check();

		const enrollButton = page.getByTestId('enroll-submit');
		await enrollButton.click();
		await expect(enrollButton).toBeDisabled();
		await enrollButton.dispatchEvent('click');
		await expect(page.getByTestId('enroll-server')).toHaveCount(0);
		expect(harness.enrollCalls).toHaveLength(1);
	});

	test('mid-test edit invalidates the pending attempt, releases the button, and stale answers arm nothing', async ({
		page
	}) => {
		const harness = await armMocks(page);
		// Reviewer-regression (R2): the pre-fix finally clause
		// `if (seq === runSeq) testing = false` never released the button when
		// a mid-flight edit bumped runSeq — Test stayed wedged in "Testing"
		// until the drawer closed.
		harness.setTestDelay(700);
		await openEnrollment(page);
		await fillTarget(page);

		const testButton = page.getByTestId('enroll-test-connection');

		// Attempt A: discovery held pending.
		await testButton.click();
		await expect(testButton).toBeDisabled();

		// Edit host AND user mid-flight: invalidates the pending discovery.
		await page.getByLabel('SSH host').fill('moved.example.internal');
		await page.getByLabel('SSH user').fill('deploy2');

		// A settles late: answer discarded (seq guard) AND the button is
		// released for a NEW attempt — no permanent wedge.
		await expect(testButton).toBeEnabled({ timeout: 5_000 });
		await expect(testButton).toHaveText('Test connection');
		// The stale discovery armed nothing: no candidates, notice, result,
		// no re-enabled trust or enroll.
		await expect(page.getByTestId('enroll-host-key-candidates')).toHaveCount(0);
		await expect(page.getByTestId('enroll-test-notice')).toHaveCount(0);
		await expect(page.getByTestId('enroll-test-result')).toHaveCount(0);
		await expect(trustCheckbox(page)).toBeDisabled();
		await expect(page.getByTestId('enroll-submit')).toBeDisabled();
		// The settle fired no new request; only A's discovery exists.
		expect(harness.testCalls).toHaveLength(1);

		// New attempt B on the edited endpoint completes the guided flow.
		harness.setTestDelay(0);
		await testButton.click();
		await expect(page.getByTestId('enroll-test-result')).toBeVisible();
		expect(harness.testCalls).toHaveLength(3); // B: discovery + auto pinned
		expect(harness.testCalls[1]).toMatchObject({
			ssh: { host: 'moved.example.internal', port: 2202, user: 'deploy2' },
			host_key: null
		});
		expect(harness.testCalls[2]).toMatchObject({ host_key: FP_A });

		// Analogous PINNED race: B's guided flow left the candidate armed, so
		// the next click runs the PINNED test directly. Hold it, edit
		// mid-flight, settle -> stale pinned answer discarded, nothing wedges.
		harness.setTestDelay(400);
		await testButton.click(); // C: pinned attempt, delayed …
		// …wait until C's pinned call is recorded (in flight), then invalidate
		// it before its answer arrives.
		await expect.poll(() => harness.testCalls.length).toBe(4);
		await page.getByLabel('SSH host').fill('final.example.internal');
		// C's pinned answer settles after the edit: discarded.
		await expect(testButton).toBeEnabled({ timeout: 5_000 });
		await expect(testButton).toHaveText('Test connection');
		await expect(page.getByTestId('enroll-test-result')).toHaveCount(0);
		await expect(page.getByTestId('enroll-host-key-candidates')).toHaveCount(0);
		await expect(trustCheckbox(page)).toBeDisabled();
		await expect(page.getByTestId('enroll-submit')).toBeDisabled();

		// A fresh attempt after the race still completes honestly.
		harness.setTestDelay(0);
		await testButton.click();
		await expect(page.getByTestId('enroll-test-result')).toBeVisible();
		expect(harness.testCalls).toHaveLength(6);
		expect(harness.testCalls[4]).toMatchObject({
			ssh: { host: 'final.example.internal' },
			host_key: null
		});
		expect(harness.testCalls[5]).toMatchObject({ host_key: FP_A });
	});
});

test.describe('guided enrollment at 360x640 (so-03)', () => {
	test.use({ viewport: { width: 360, height: 640 } });

	test.beforeEach(async ({ page }) => {
		await armMocks(page);
	});

	test('the guided flow stays usable with no horizontal overflow', async ({ page }) => {
		await openEnrollment(page);

		// Public key wraps inside the viewport; the document does not scroll
		// horizontally with a long key/fingerprint rendered.
		await expect(page.getByTestId('enroll-controller-public-key')).toBeVisible();
		expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
			360
		);

		await fillTarget(page);
		await page.getByTestId('enroll-test-connection').click();
		await expect(page.getByTestId('enroll-test-result')).toBeVisible();
		expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
			360
		);
	});
});
