import { expect, test, type Locator, type Page, type Route } from '@playwright/test';

/**
 * 01-desktop-shell — real-browser proofs for the composed desktop shell on
 * the ACTUAL app route (frontend/src/routes/+page.svelte rendering
 * DesktopShell): no virtual fixtures, no backend — the production build the
 * webServer serves is the exercised entrypoint.
 *
 * Scenario coverage (brief "feature-gate coverage", runtime owner 01-verify;
 * windowed-workspace ADR §10.1 PT1/PT2/PT5, §10.2 NT5, §5 SSR isolation,
 * contracts.md C08 rule 5, C11):
 *  - the SvelteKit route mounts the shell: SSR renders the empty desktop
 *    (launcher only), hydration mounts the shell inside the reviewed
 *    01-web component host;
 *  - open two labeled fixture panels from the desktop; independent
 *    draggable frames + panel hosts with explicit target labels;
 *  - drag/focus/close/reopen with keyboard and pointer (interactions
 *    counters prove content stays mounted across focus/drag and remounts
 *    fresh on reopen);
 *  - changing the selected server fixture never retargets open windows;
 *  - workspace state is per-session (second browser context starts empty);
 *  - only benign fixtures: the journey performs zero API requests (the one
 *    deliberate exception is the live-nav proof, which sets the readable
 *    `mai_csrf` cookie and mocks exactly the listed-profiles + memory-status
 *    reads to compose the REAL live entries into the REAL ServerTreeNav).
 *
 * Known limits (recorded in the card evidence): minimize/maximize chrome is
 * rendered by WindowFrame but its transitions belong to 01-window-presentation
 * and are deliberately not wired here; keyboard Move mode is a later card.
 */

const NOTES = 'fixture-notes';
const PREFS = 'fixture-prefs';
const SERVER_ALPHA_ID = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';

/** A window frame locator for one panel kind (frames carry role="dialog"). */
function frame(page: Page, kind: string) {
	return page.locator(`.mai-window-frame[data-panel-kind="${kind}"]`);
}

async function openPanel(page: Page, kind: string): Promise<void> {
	await page.getByTestId(`open-${kind}`).click();
	await expect(frame(page, kind)).toHaveCount(1);
}

test('the route server-renders the empty desktop shell and mounts it client-side', async ({
	page
}) => {
	// D4/§5: the SSR document is the shell with an EMPTY workspace — the
	// SideDrawer workspace navigation (Multi-Server accordion tree), zero
	// window/panel DOM, no domain content in the route.
	const ssr = await (await page.request.get('/')).text();
	expect(ssr).toContain('data-testid="desktop"');
	expect(ssr).toContain('data-testid="server-tree-nav"');
	expect(ssr).toContain('data-testid="bottom-chat-dock"');
	expect(ssr).not.toContain('data-panel-kind');
	expect(ssr).not.toContain('mai-fixture-panel');

	await page.goto('/');
	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Machina');
	await expect(page.getByTestId('component-host')).toHaveAttribute('data-mounted', 'true');

	const select = page.getByTestId('server-select');
	await expect(select).toBeVisible();
	await expect(select.locator('option')).toHaveText(['Active: Server Alpha', 'Active: Server Beta']);
	await expect(page.getByTestId('server-tree-nav')).toBeVisible();
	await expect(page.getByTestId('open-' + NOTES)).toContainText('Open Notes — Server Alpha');
	await expect(page.getByTestId('open-' + PREFS)).toContainText('Open Preferences — Global');
	await expect(frame(page, NOTES)).toHaveCount(0);
	await expect(frame(page, PREFS)).toHaveCount(0);
});

test('opens two labeled fixture panels with independent frames and panel hosts', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	await openPanel(page, PREFS);

	const notes = frame(page, NOTES);
	const prefs = frame(page, PREFS);
	await expect(notes.getByRole('heading')).toHaveText('Notes — Server Alpha');
	await expect(prefs.getByRole('heading')).toHaveText('Preferences — Global');

	// Explicit target labels at open time (launcher status mirrors them).
	await expect(page.getByTestId('desktop-status')).toHaveText('Opened Preferences — Global.');

	// Independent panel hosts: one mounted fixture body per frame.
	const notesId = await notes.getAttribute('data-window-id');
	const prefsId = await prefs.getAttribute('data-window-id');
	expect(notesId).toBeTruthy();
	expect(prefsId).toBeTruthy();
	expect(notesId).not.toBe(prefsId);
	await expect(notes.getByTestId('fixture-panel')).toHaveAttribute('data-fixture-panel', NOTES);
	await expect(prefs.getByTestId('fixture-panel')).toHaveAttribute('data-fixture-panel', PREFS);
	await expect(notes.getByTestId('fixture-panel')).toContainText(
		'benign placeholder, no live data.'
	);
	await expect(notes.getByTestId('fixture-panel')).toContainText(`Pinned target: server ${SERVER_ALPHA_ID}`);
	await expect(prefs.getByTestId('fixture-panel')).toContainText('Pinned target: global');

	// Open focuses: prefs (opened last) is on top, notes below.
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await expect(notes).toHaveAttribute('data-focused', 'false');
	await expect
		.poll(() => prefs.evaluate((el) => el.style.zIndex))
		.toBe(await notes.evaluate((el) => String(Number(el.style.zIndex) + 1)));
});

test('activation by pointer and keyboard raises windows without remounting content', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	await openPanel(page, PREFS);
	const notes = frame(page, NOTES);
	const prefs = frame(page, PREFS);
	await expect(notes.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 0');

	// Pointer: clicking the inactive window's body raises it (PT2).
	await notes.getByTestId('fixture-panel').click();
	await expect(notes).toHaveAttribute('data-focused', 'true');
	await expect(prefs).toHaveAttribute('data-focused', 'false');

	// Keyboard: Tab from the body control to the other window's focusable
	// title bar — focusin raises that window (PT2 keyboard half) and focus
	// STAYS on the title (the stable DOM order must not reparent the focused
	// frame, which would drop focus to the document).
	await notes.getByTestId('fixture-interactions').click();
	await page.keyboard.press('Tab');
	await expect(prefs.getByRole('heading')).toBeFocused();
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await expect(notes).toHaveAttribute('data-focused', 'false');

	// Raising never remounts panel content: counters survive both raises.
	await expect(notes.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 1');
	await expect(prefs.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 0');
});

test('title-bar drag moves a window through the shell and clamps at the viewport', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	const notes = frame(page, NOTES);
	const bar = notes.locator('.mai-window-frame__titlebar');
	await expect(notes).toHaveAttribute('data-focused', 'true');

	// PT1 through the composed shell: drag commits geometry via state.move.
	await expect(notes).toHaveAttribute('data-focused', 'true');
	const before = await bar.boundingBox();
	expect(before).not.toBeNull();
	await page.mouse.move(before!.x + before!.width / 2, before!.y + before!.height / 2);
	await page.mouse.down();
	await page.mouse.move(before!.x + before!.width / 2 + 90, before!.y + before!.height / 2 + 70, {
		steps: 5
	});
	await page.mouse.up();
	await expect(notes).toHaveAttribute('data-focused', 'true');
	await expect
		.poll(() => notes.evaluate((el) => ({ x: el.style.left, y: el.style.top })))
		.toEqual({ x: '114px', y: '94px' });

	// Content stayed mounted through the gesture.
	await expect(notes.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 0');

	// NT3: dragging far past every edge clamps so a ≥64×32 title-bar area
	// stays visible and hit-testable inside the viewport.
	const width = page.viewportSize()?.width ?? 1280;
	const height = page.viewportSize()?.height ?? 720;
	await page.mouse.move(before!.x + before!.width / 2, before!.y + before!.height / 2);
	await page.mouse.down();
	await page.mouse.move(-2000, -2000, { steps: 5 });
	await page.mouse.up();
	const box = await notes.boundingBox();
	expect(box).not.toBeNull();
	expect(box!.x).toBeLessThanOrEqual(width - 64);
	expect(box!.x + box!.width).toBeGreaterThanOrEqual(64);
	expect(box!.y).toBeLessThanOrEqual(height - 32);
	expect(box!.y + box!.height).toBeGreaterThanOrEqual(32);
	await expect(notes.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 0');
});

test('close removes the window and its host; reopen mounts a fresh instance', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	const notes = frame(page, NOTES);
	const firstId = await notes.getAttribute('data-window-id');
	await notes.getByTestId('fixture-interactions').click();
	await expect(notes.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 1');

	// PT5: close removes frame + panel host; the workspace keeps running.
	await notes.getByRole('button', { name: 'Close' }).click();
	await expect(frame(page, NOTES)).toHaveCount(0);
	await expect(page.getByTestId('fixture-panel')).toHaveCount(0);
	await expect(page.getByTestId('desktop-status')).toHaveText('Opened Notes — Server Alpha.');

	// Reopen: a NEW view identity with fresh (remounted) content.
	await page.getByTestId('open-' + NOTES).click();
	await expect(frame(page, NOTES)).toHaveCount(1);
	const reopened = frame(page, NOTES);
	expect(await reopened.getAttribute('data-window-id')).not.toBe(firstId);
	await expect(reopened.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 0');
});

test('keyboard close via the frame control; launcher reopens with the keyboard', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, PREFS);
	await openPanel(page, NOTES);

	/**
	 * Walks REAL Tab presses until `target` has focus (bounded; no
	 * programmatic focus() — the keyboard path is the behavior under test).
	 */
	async function tabUntil(target: Locator, maxHops: number): Promise<void> {
		for (let hop = 0; hop < maxHops; hop += 1) {
			if (await target.evaluate((el) => document.activeElement === el)) return;
			await page.keyboard.press('Tab');
		}
		await expect(target).toBeFocused();
	}

	const prefs = frame(page, PREFS);

	// Keyboard: Tab order reaches the prefs title bar; focusin raises the
	// window (PT2 keyboard half).
	await tabUntil(prefs.getByRole('heading'), 8);
	await expect(prefs).toHaveAttribute('data-focused', 'true');

	// Three controls follow the title: Minimize, Maximize, Close.
	await tabUntil(prefs.getByRole('button', { name: 'Close' }), 4);
	await page.keyboard.press('Enter');
	await expect(frame(page, PREFS)).toHaveCount(0);

	// Notes was never touched by the prefs close (independent hosts).
	const notes = frame(page, NOTES);
	await expect(notes).toHaveCount(1);
	await expect(notes.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 0');

	// Reopen prefs from the launcher with the keyboard. Arrival: this
	// headless Chromium drops the sequential-navigation start point after a
	// focused control is removed, so Tab-from-body into the launcher is
	// unreliable here (quirk recorded in the card evidence); .focus() stands
	// in for the Tab arrival while Enter stays the real keyboard activation.
	const openPrefs = page.getByTestId('open-' + PREFS);
	await openPrefs.focus();
	await page.keyboard.press('Enter');
	await expect(frame(page, PREFS)).toHaveCount(1);
	await expect(frame(page, PREFS).getByTestId('fixture-interactions')).toHaveText(
		'Fixture interactions: 0'
	);
});

test('singleton open focuses the live instance instead of duplicating it', async ({ page }) => {
	await page.goto('/');
	await openPanel(page, PREFS);
	const prefs = frame(page, PREFS);
	const id = await prefs.getAttribute('data-window-id');

	await page.getByTestId('open-' + PREFS).click();
	await expect(frame(page, PREFS)).toHaveCount(1);
	await expect(prefs).toHaveAttribute('data-window-id', id!);
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await expect(page.getByTestId('desktop-status')).toHaveText('Opened Preferences — Global.');
});

test('changing the launcher server never retargets open windows (NT5)', async ({ page }) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	// DOM order = stacking order: alpha is opened first and is never raised,
	// so it stays the first notes frame even after the beta window opens.
	const alpha = frame(page, NOTES).first();
	const alphaId = await alpha.getAttribute('data-window-id');
	await alpha.getByTestId('fixture-interactions').click();
	await expect(alpha.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 1');

	// The launcher selection changes; existing windows are not rewritten.
	await page.getByTestId('server-select').selectOption({ label: 'Active: Server Beta' });
	await expect(page.getByTestId('open-' + NOTES)).toContainText('Open Notes — Server Beta');
	await expect(alpha.getByRole('heading')).toHaveText('Notes — Server Alpha');
	await expect(alpha.getByTestId('fixture-panel')).toContainText(
		`Pinned target: server ${SERVER_ALPHA_ID}`
	);
	await expect(alpha).toHaveAttribute('data-window-id', alphaId!);

	// A subsequent open pins the NEW selection as its own explicit scope.
	await page.getByTestId('open-' + NOTES).click();
	await expect(frame(page, NOTES)).toHaveCount(2);
	const beta = frame(page, NOTES).last();
	await expect(beta).not.toHaveAttribute('data-window-id', alphaId!);
	await expect(beta.getByRole('heading')).toHaveText('Notes — Server Beta');
	await expect(beta.getByTestId('fixture-panel')).toContainText(
		'Pinned target: server b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60'
	);
	await expect(beta).toHaveAttribute('data-focused', 'true');

	// The alpha window kept its scope AND its live content throughout.
	await expect(alpha.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 1');
	await expect(beta.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 0');
});

test('workspace state is per-session: a second browser context starts empty', async ({
	page,
	browser
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	await expect(frame(page, NOTES)).toHaveCount(1);

	const other = await browser.newContext();
	const otherPage = await other.newPage();
	await otherPage.goto('/');
	await expect(otherPage.getByTestId('component-host')).toHaveAttribute('data-mounted', 'true');
	await expect(frame(otherPage, NOTES)).toHaveCount(0);
	await expect(frame(page, NOTES)).toHaveCount(1);
	await other.close();
});

test('the fixture desktop performs no API requests', async ({ page }) => {
	const urls: string[] = [];
	page.on('request', (request) => urls.push(request.url()));

	await page.goto('/');
	await openPanel(page, NOTES);
	await openPanel(page, PREFS);
	await frame(page, NOTES).getByRole('button', { name: 'Close' }).click();
	await page.getByTestId('open-' + NOTES).click();

	expect(urls.length).toBeGreaterThan(0);
	expect(urls.filter((url) => url.includes('/api'))).toEqual([]);
});

test('live nav: Global / Fleet lists Memory only there and opens it with the global scope', async ({
	page
}) => {
	// A readable `mai_csrf` cookie is the live-attach probe; the one listed
	// profiles request is mocked, so the REAL live entries compose into the
	// REAL ServerTreeNav on the real route.
	await page.context().addCookies([
		{ name: 'mai_csrf', value: 'fixture-csrf', url: 'http://127.0.0.1:4173' }
	]);
	await page.route('**/api/v1/servers', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				servers: [
					{
						schema_version: 1,
						server_id: SERVER_ALPHA_ID,
						name: 'Server Alpha',
						prompt_revision: 1,
						prompt_text: 'fixture',
						environment: 'linux_ssh',
						ssh: {
							host: 'alpha.example',
							port: 22,
							user: 'mai',
							host_key: 'k',
							credential_ref: 'MAI_SSH_KEY'
						},
						privilege: 'sudo',
						capabilities: ['memory.recall'],
						revision: 1,
						sha256: 'f'.repeat(64),
						projection: { revision: 1, stale: false }
					}
				]
			})
		})
	);
	// The Memory panel's mount probe answers honestly for any target.
	await page.route('**/api/v1/servers/*/memory/status', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				status: 'ok',
				code: null,
				provider: 'hindsight',
				operation: 'status',
				api_version: 'v1.2.3',
				error_type: null
			})
		})
	);
	// uir-u4: the Runbook Catalog window reads the global catalog route.
	await page.route('**/api/v1/applications/catalog**', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ runbooks: [], categories: [], limit: 10, offset: 0, total: 0 })
		})
	);
	await page.route('**/api/v1/models/profiles', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				profiles: [
					{
						schema_version: 1,
						name: 'glm53flash',
						connection: 'litellm',
						model: 'z-ai-coding/glm-5.3-flash'
					}
				]
			})
		})
	);
	await page.route('**/api/v1/models/selection', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				schema_version: 1,
				connection: 'litellm',
				profile: 'glm53flash'
			})
		})
	);

	await page.goto('/');
	await expect(page.getByTestId('server-tree-nav')).toBeVisible();

	// D09: the dock IS the session panel. On a live host with no open
	// session it renders the honest empty/create state (no legacy profile
	// select — that surface died with the ChatPanel); the model profile
	// vocabulary for session turns is proven by the session suites.
	await expect(
		page.getByTestId('bottom-chat-dock').getByTestId('session-chat-panel')
	).toBeVisible();
	await expect(
		page.getByTestId('bottom-chat-dock').getByTestId('session-config-error')
	).toHaveCount(0);

	// Global / Fleet lists the Memory entry — and ONLY once, globally.
	const memoryItem = page.getByTestId('open-knowledge-models');
	await expect(memoryItem).toContainText('Open Memory — Global');
	await expect(page.getByTestId('open-knowledge-models-' + SERVER_ALPHA_ID)).toHaveCount(0);
	await expect(page.getByTestId('open-model-settings-' + SERVER_ALPHA_ID)).toHaveCount(0);

	// The per-server Documents entry stays per-server; no Memory/Model-settings
	// item repeats inside a server accordion (they are Global / Fleet kinds).
	await expect(page.getByTestId('open-documents')).toContainText('Open Documents — Server Alpha');

	// uir u2-terminal: the Execution panel presents as Terminal in the nav.
	await expect(page.getByTestId('open-execution')).toContainText('Open Terminal — Server Alpha');

	// The global entry opens the Memory window pinned to the GLOBAL scope.
	await memoryItem.click();
	await expect(frame(page, 'knowledge-models')).toHaveCount(1);
	await expect(frame(page, 'knowledge-models').locator('.mai-window-frame__title')).toHaveText(
		'Memory — Global'
	);
	await expect(page.getByTestId('desktop-status')).toHaveText('Opened Memory — Global.');
	// The mounted panel probed availability through the real client.
	await expect(frame(page, 'knowledge-models').getByTestId('memory-status-ok')).toContainText(
		'v1.2.3'
	);

	// uir-u4: the Runbook Catalog entry lives under Global / Fleet only,
	// never inside a server accordion, and opens with the global scope.
	const runbooksItem = page.getByTestId('open-runbooks');
	await expect(runbooksItem).toContainText('📖');
	await expect(runbooksItem).toContainText('Open Runbook Catalog — Global');
	await expect(page.getByTestId('open-runbooks-' + SERVER_ALPHA_ID)).toHaveCount(0);
	await runbooksItem.click();
	await expect(frame(page, 'runbooks')).toHaveCount(1);
	await expect(frame(page, 'runbooks').locator('.mai-window-frame__title')).toHaveText(
		'Runbook Catalog — Global'
	);
	await expect(page.getByTestId('desktop-status')).toHaveText('Opened Runbook Catalog — Global.');
	// The mounted panel read the catalog through the real client and renders
	// the honest empty state for the mocked empty page.
	await expect(frame(page, 'runbooks').getByTestId('catalog-empty')).toBeVisible();
});

test('live selected server survives reload, pins panel scope, and falls back when stale', async ({
	page
}) => {
	const SERVER_BETA_ID = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
	const SELECTED_KEY = 'mai.workspace.selected-server.v1';
	const profileFor = (serverId: string, name: string) => ({
		schema_version: 1,
		server_id: serverId,
		name,
		prompt_revision: 1,
		prompt_text: 'fixture',
		environment: 'linux_ssh',
		ssh: { host: `${name}.example`, port: 22, user: 'mai', host_key: 'k', credential_ref: 'MAI_SSH_KEY' },
		privilege: 'sudo',
		capabilities: ['memory.recall'],
		revision: 1,
		sha256: 'f'.repeat(64),
		projection: { revision: 1, stale: false }
	});

	// The readable `mai_csrf` cookie is the live-attach probe; the profiles
	// list is mocked with TWO enrolled servers so a first-server default and
	// a non-default choice are distinguishable.
	await page.context().addCookies([
		{ name: 'mai_csrf', value: 'fixture-csrf', url: 'http://127.0.0.1:4173' }
	]);
	await page.route('**/api/v1/servers', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				servers: [
					profileFor(SERVER_ALPHA_ID, 'Server Alpha'),
					profileFor(SERVER_BETA_ID, 'Server Beta')
				]
			})
		})
	);
	await page.route('**/api/v1/models/profiles', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				profiles: [
					{
						schema_version: 1,
						name: 'glm53flash',
						connection: 'litellm',
						model: 'z-ai-coding/glm-5.3-flash'
					}
				]
			})
		})
	);
	await page.route('**/api/v1/models/selection', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				schema_version: 1,
				connection: 'litellm',
				profile: 'glm53flash'
			})
		})
	);
	// The Documents panel's list route: its pinned server_id is the scope proof.
	await page.route('**/api/v1/docs**', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				server_id: new URL(route.request().url()).searchParams.get('server_id'),
				app_id: null,
				namespace: null,
				documents: [],
				total: 0,
				limit: 10,
				offset: 0
			})
		})
	);

	await page.goto('/');
	await expect(page.getByTestId('server-tree-nav')).toBeVisible();
	// First enrolled server is the honest default before any stored choice.
	await expect(page.getByTestId('server-select')).toHaveValue(SERVER_ALPHA_ID);

	// The operator selects the NON-default server; the choice persists
	// immediately as the bare UUID.
	await page.getByTestId('server-select').selectOption({ label: 'Active: Server Beta' });
	await expect(page.getByTestId('server-select')).toHaveValue(SERVER_BETA_ID);
	expect(await page.evaluate((key) => window.localStorage.getItem(key), SELECTED_KEY)).toBe(
		SERVER_BETA_ID
	);

	// RELOAD: the restored selection is Server Beta, NOT the first server —
	// and connectLive no longer overrides it.
	await page.reload();
	await expect(page.getByTestId('server-tree-nav')).toBeVisible();
	await expect(page.getByTestId('server-select')).toHaveValue(SERVER_BETA_ID);

	// The open-panel scope follows the restored selection: the Documents
	// launcher carries Beta's label and its request is pinned to Beta's id.
	const docUrls: string[] = [];
	page.on('request', (request) => {
		if (request.url().includes('/api/v1/docs')) docUrls.push(request.url());
	});
	await expect(page.getByTestId('open-documents')).toContainText('Open Documents — Server Beta');
	await page.getByTestId('open-documents').click();
	await expect(frame(page, 'documents')).toHaveCount(1);
	await expect(
		page.locator(`.mai-window-frame[data-panel-kind="documents"] .mai-window-frame__title`)
	).toHaveText('Documents — Server Beta');
	// The documents library loads on demand; the refresh button issues the
	// pinned-scope list request.
	await frame(page, 'documents').getByTestId('library-refresh').click();
	expect(docUrls.some((url) => url.includes(`server_id=${SERVER_BETA_ID}`))).toBe(true);
	// Selection never retargets the already-open window on later changes.
	await page.getByTestId('server-select').selectOption({ label: 'Active: Server Alpha' });
	await expect(
		page.locator(`.mai-window-frame[data-panel-kind="documents"] .mai-window-frame__title`)
	).toHaveText('Documents — Server Beta');

	// A DELETED/unknown stored server falls back deterministically to the
	// first enrolled server and repairs the stored value.
	await page.evaluate(
		([key, id]) => window.localStorage.setItem(key, id),
		[SELECTED_KEY, '11111111-2222-4333-8444-555555555555'] as const
	);
	await page.reload();
	await expect(page.getByTestId('server-tree-nav')).toBeVisible();
	await expect(page.getByTestId('server-select')).toHaveValue(SERVER_ALPHA_ID);
	expect(await page.evaluate((key) => window.localStorage.getItem(key), SELECTED_KEY)).toBe(
		SERVER_ALPHA_ID
	);

	// A selection from ANOTHER session (foreign marker) never restores: the
	// session reconciliation clears it together with the layout document.
	await page.evaluate(
		([selectedKey, sessionKey, betaId]) => {
			window.localStorage.setItem(selectedKey, betaId);
			window.localStorage.setItem(sessionKey, 'foreign-session');
		},
		[SELECTED_KEY, 'mai.workspace.session.v1', SERVER_BETA_ID] as const
	);
	await page.reload();
	await expect(page.getByTestId('server-tree-nav')).toBeVisible();
	// The foreign selection was cleared (never restored); the shell falls
	// back to the first enrolled server and repairs the stored value.
	await expect(page.getByTestId('server-select')).toHaveValue(SERVER_ALPHA_ID);
	expect(await page.evaluate((key) => window.localStorage.getItem(key), SELECTED_KEY)).toBe(
		SERVER_ALPHA_ID
	);
	expect(
		await page.evaluate(
			(key) => window.localStorage.getItem(key),
			'mai.workspace.session.v1'
		)
	).toBeNull();
});

test('live jobs panel resolves the pinned server name through the real composition', async ({
	page
}) => {
	const BETA = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
	const profileFor = (serverId: string, name: string) => ({
		schema_version: 1,
		server_id: serverId,
		name,
		prompt_revision: 1,
		prompt_text: 'fixture',
		environment: 'linux_ssh',
		ssh: {
			host: `${name}.example`,
			port: 22,
			user: 'mai',
			host_key: 'k',
			credential_ref: 'MAI_SSH_KEY'
		},
		privilege: 'sudo',
		capabilities: ['memory.recall'],
		revision: 1,
		sha256: 'f'.repeat(64),
		projection: { revision: 1, stale: false }
	});
	await page.context().addCookies([
		{ name: 'mai_csrf', value: 'fixture-csrf', url: 'http://127.0.0.1:4173' }
	]);
	await page.route('**/api/v1/servers', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				servers: [profileFor(SERVER_ALPHA_ID, 'Server Alpha'), profileFor(BETA, 'Server Beta')]
			})
		})
	);
	// The REAL composed readProfile path: the jobs panel's name seam calls
	// GET /api/v1/servers/{id} through the base client.
	let profileReads = 0;
	await page.route('**/api/v1/servers/*', (route) => {
		if (route.request().method() !== 'GET') return route.fallback();
		const url = new URL(route.request().url());
		if (url.pathname.endsWith('/memory/status') || url.pathname.endsWith('/terminal/info')) {
			return route.fallback();
		}
		profileReads += 1;
		const requested = decodeURIComponent(url.pathname.split('/').pop() ?? '');
		return route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(profileFor(requested, requested === BETA ? 'Server Beta' : 'Server Alpha'))
		});
	});
	await page.route('**/api/v1/jobs/runs?*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ runs: [], limit: 10, offset: 0, total: 0 })
		})
	);
	await page.route('**/api/v1/schedules?*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ schedules: [], limit: 10, offset: 0, total: 0 })
		})
	);
	await page.route('**/api/v1/models/profiles', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ profiles: [] })
		})
	);
	await page.route('**/api/v1/models/selection', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ schema_version: 1, connection: 'litellm', profile: '' })
		})
	);

	await page.goto('/');
	await expect(page.getByTestId('server-tree-nav')).toBeVisible();
	// Select the NON-default server, then open the jobs panel pinned to it.
	await page.getByTestId('server-select').selectOption({ label: 'Active: Server Beta' });
	await page.getByTestId('open-jobs').click();
	const jobsRoot = page.locator(`.mai-window-frame[data-panel-kind="jobs"]`);
	await expect(jobsRoot).toHaveCount(1);
	// The panel shows the resolved NAME of ITS pinned scope (Beta) — resolved
	// through the real readProfile composition, not a second collection load.
	await expect(jobsRoot.getByTestId('jobs-scope-name')).toHaveText('Server Beta');
	await expect(jobsRoot.getByTestId('jobs-scope')).toHaveText(`Server ${BETA.slice(0, 8)}`);
	await expect(jobsRoot.getByTestId('runs-empty')).toHaveText(
		'No runs are recorded for this server.'
	);
	expect(profileReads).toBeGreaterThanOrEqual(1);
});

test('the chat column is always visible, honest offline, and performs no API requests', async ({
	page
}) => {
	const urls: string[] = [];
	page.on('request', (request) => urls.push(request.url()));

	await page.goto('/');
	const column = page.getByTestId('bottom-chat-dock');
	await expect(column).toBeVisible();
	// The column is NOT a registry window: no panel kind, no window frame.
	expect(await column.getAttribute('data-panel-kind')).toBeNull();
	await expect(column.locator('.mai-window-frame')).toHaveCount(0);
	await expect(frame(page, 'chat')).toHaveCount(0);

	// D09: the session panel IS the column. Offline the service bag carries
	// no session methods, so the panel's structural narrowing fails closed
	// with the honest alert — never a fabricated composer or transcript.
	await expect(column.getByTestId('session-chat-panel')).toBeVisible();
	await expect(column.getByTestId('session-config-error')).toBeVisible();
	// The drawer's New Chat focuses the column without any effect offline.
	await page.getByTestId('drawer-new-chat').click();
	await expect(column.getByTestId('session-chat-panel')).toBeVisible();

	expect(urls.filter((url) => url.includes('/api'))).toEqual([]);
});

for (const viewport of [{width: 1280, height: 800}, {width: 360, height: 640}]) {
	test(`offline chat column ${viewport.width}: honest panel, compact centered hero card, contained`, async ({page}) => {
		await page.setViewportSize(viewport);
		const requests: string[] = [];
		page.on('request', r => requests.push(r.url()));
		await page.goto('/');
		// Existing user-controlled shell navigation, not a geometry override.
		if (viewport.width === 360) {
			await page.getByTestId('side-drawer-toggle').click();
			// The drawer rail ANIMATES its width/flex-basis down to 3.25rem;
			// the hero card is content-sized, so its height is only stable
			// once the rail settles (mid-animation the narrower stage wraps
			// the honest panel's text taller). Poll for the settled rail.
			await expect
				.poll(() =>
					page.evaluate(
						() => document.querySelector('.mai-side-drawer')!.getBoundingClientRect().width
					)
				)
				.toBeLessThanOrEqual(53);
		}
		const column = page.getByTestId('bottom-chat-dock');
		// The session panel renders its honest unwired state offline (D09:
		// the legacy in-memory composer is deleted, composer reachability at
		// both viewports is proven against the REAL backend by the
		// session-virtual-history suite).
		await expect(column.getByTestId('session-chat-panel')).toBeVisible();
		await expect(column.getByTestId('session-config-error')).toBeVisible();

		// Geometry (CCC S1 / CHC H1): the column BOX stays centered in the
		// stage, width-bounded and stretched between the stage paddings —
		// but it is now the transparent positioning host; the blended CARD
		// (dock-body) is COMPACT and centered inside it in hero (no
		// session): the surface hugs the honest panel, never a full-height
		// empty slab.
		const geo = await page.evaluate(() => {
			const stage = document.querySelector('[data-testid="desktop"]')!.parentElement!;
			const columnEl = document.querySelector('[data-testid="bottom-chat-dock"]')!;
			const cardEl = columnEl.querySelector('.mai-desktop-shell__dock-body')!;
			const sr = stage.getBoundingClientRect();
			const cr = columnEl.getBoundingClientRect();
			const sc = getComputedStyle(stage);
			const cc = getComputedStyle(columnEl);
			const cd = cardEl.getBoundingClientRect();
			return {
				stage: {
					x: sr.x,
					y: sr.y,
					width: sr.width,
					height: sr.height,
					padTop: parseFloat(sc.paddingTop),
					padBottom: parseFloat(sc.paddingBottom),
					padSide: parseFloat(sc.paddingLeft)
				},
				column: {
					x: cr.x,
					y: cr.y,
					right: cr.right,
					bottom: cr.bottom,
					width: cr.width,
					height: cr.height,
					padTop: parseFloat(cc.paddingTop),
					padBottom: parseFloat(cc.paddingBottom),
					padX: parseFloat(cc.paddingLeft),
					isHero: columnEl.classList.contains('mai-desktop-shell__dock--hero')
				},
				card: {
					top: cd.top,
					bottom: cd.bottom,
					height: cd.height,
					width: cd.width
				},
				scrollW: document.documentElement.scrollWidth
			};
		});

		// Hero state class drives the compact card (CHC H1).
		expect(geo.column.isHero).toBe(true);

		// Centered: the column's horizontal center matches the stage's
		// (the stage, not the viewport — the drawer rail offsets the stage).
		const columnCenter = geo.column.x + geo.column.width / 2;
		const stageCenter = geo.stage.x + geo.stage.width / 2;
		expect(Math.abs(columnCenter - stageCenter), 'column centered in stage').toBeLessThanOrEqual(1);

		// Width bounded (CCC S1): strictly narrower than the stage — canvas
		// visible on both sides — capped at 880px at desktop width; on
		// pocket viewports the column is the stage minus page padding.
		expect(geo.column.width).toBeLessThan(geo.stage.width);
		if (viewport.width === 1280) {
			expect(geo.column.width).toBeLessThanOrEqual(880);
		} else {
			expect(
				Math.abs(geo.column.width - (geo.stage.width - 2 * geo.stage.padSide)),
				'pocket width = stage minus page padding'
			).toBeLessThanOrEqual(1);
		}

		// The column BOX still stretches between the stage paddings (top →
		// bottom page padding) — it is the positioning host.
		expect(
			Math.abs(geo.column.y - (geo.stage.y + geo.stage.padTop)),
			'column top at stage content top'
		).toBeLessThanOrEqual(1);
		expect(
			Math.abs(geo.column.height - (geo.stage.height - geo.stage.padTop - geo.stage.padBottom)),
			'column fills stage height minus paddings'
		).toBeLessThanOrEqual(1);

		// CHC H1: in hero the CARD is compact (a small fraction of the
		// column, hugging the honest panel — never a full-height slab) and
		// vertically centered inside the column's content box.
		const contentTop = geo.column.y + geo.column.padTop;
		const contentBottom = geo.column.bottom - geo.column.padBottom;
		expect(geo.card.height).toBeLessThan((contentBottom - contentTop) / 2);
		expect(
			Math.abs(geo.card.top - contentTop - (contentBottom - geo.card.bottom)),
			'hero card vertically centered in the column'
		).toBeLessThanOrEqual(1.5);
		expect(
			Math.abs(geo.card.width - (geo.column.width - 2 * geo.column.padX)),
			'hero card fills the column content width'
		).toBeLessThanOrEqual(2);

		// Contained: nothing paints outside the viewport and no horizontal
		// page overflow at either viewport.
		const panelBox = await column.getByTestId('session-chat-panel').boundingBox();
		expect(panelBox).not.toBeNull();
		expect(panelBox!.y).toBeGreaterThanOrEqual(0);
		expect(panelBox!.y + panelBox!.height).toBeLessThanOrEqual(viewport.height);
		expect(panelBox!.x + panelBox!.width).toBeLessThanOrEqual(viewport.width);
		expect(geo.column.right).toBeLessThanOrEqual(viewport.width);
		expect(geo.column.bottom).toBeLessThanOrEqual(viewport.height);
		expect(geo.scrollW).toBeLessThanOrEqual(viewport.width);
		expect(requests.filter(url => url.includes('/api'))).toEqual([]);
	});
}

test('the chat column carries the reduced-motion state class only under reduce (CHC H1)', async ({
	page
}) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/');
	const column = page.getByTestId('bottom-chat-dock');
	await expect(column).toBeVisible();
	// Default motion: hero state class present, no reduced-motion gate.
	await expect(column).toHaveClass(/mai-desktop-shell__dock--hero/);
	await expect(column).not.toHaveClass(/mai-desktop-shell__dock--reduced-motion/);
	// Reduced motion: the class flips with the media query — the shell's
	// WAAPI card growth is gated on this state (instant switch instead).
	await page.emulateMedia({ reducedMotion: 'reduce' });
	await expect(column).toHaveClass(/mai-desktop-shell__dock--reduced-motion/);
	await expect(column).toHaveClass(/mai-desktop-shell__dock--hero/);
});

test('drawer Chat History is honestly empty offline (no fabricated rows)', async ({
	page
}) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/');
	const drawerSection = page.getByTestId('drawer-chat-section');
	await expect(drawerSection).toBeVisible();
	// D09: drawer rows come from the agent-session history surface only.
	// Offline there is no backend, so the honest state is zero rows and no
	// pagination control (the live rows + rename + pagination UX is proven
	// against the REAL backend by the session-virtual-history suite).
	await expect(drawerSection.getByTestId('drawer-history-item')).toHaveCount(0);
	await expect(drawerSection.getByTestId('drawer-history-load-more')).toHaveCount(0);
});

test('opened windows stack ABOVE the centered chat column and unauthorized live session redirects to /login', async ({
	page
}) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/');

	const column = page.getByTestId('bottom-chat-dock');
	await openPanel(page, NOTES);
	const notes = frame(page, NOTES);

	// The desktop canvas (`z-index: 20`) sits above the centered chat
	// column (`z-index: 5`), so any opened window renders on top of the
	// column, while empty canvas space has `pointer-events: none` so the
	// column stays interactive underneath.
	const stacking = await page.evaluate(() => {
		const desktopEl = document.querySelector('[data-testid="desktop"]');
		const dockEl = document.querySelector('[data-testid="bottom-chat-dock"]');
		const frameEl = document.querySelector('.mai-window-frame');
		return {
			desktopZ: Number(desktopEl ? getComputedStyle(desktopEl).zIndex : 0),
			desktopPointer: desktopEl ? getComputedStyle(desktopEl).pointerEvents : '',
			framePointer: frameEl ? getComputedStyle(frameEl).pointerEvents : '',
			dockZ: Number(dockEl ? getComputedStyle(dockEl).zIndex : 0)
		};
	});
	expect(stacking.desktopZ).toBeGreaterThan(stacking.dockZ);
	expect(stacking.desktopPointer).toBe('none');
	expect(stacking.framePointer).toBe('auto');

	// Drag the window directly over the centered chat column and verify
	// hit-testing lands inside the window frame rather than being covered
	// by the column.
	const columnBox = await column.boundingBox();
	const bar = notes.locator('.mai-window-frame__titlebar');
	const barBox = await bar.boundingBox();
	expect(columnBox).not.toBeNull();
	expect(barBox).not.toBeNull();

	await page.mouse.move(barBox!.x + barBox!.width / 2, barBox!.y + barBox!.height / 2);
	await page.mouse.down();
	await page.mouse.move(columnBox!.x + columnBox!.width / 2, columnBox!.y + 20, { steps: 5 });
	await page.mouse.up();

	const updatedBox = await notes.boundingBox();
	expect(updatedBox).not.toBeNull();
	const overlapX = Math.max(updatedBox!.x, columnBox!.x) + 40;
	const overlapY = Math.max(updatedBox!.y, columnBox!.y) + 20;
	const topElementIsInsideFrame = await page.evaluate(
		([x, y]) => {
			const hit = document.elementFromPoint(x, y);
			return hit !== null && hit.closest('.mai-window-frame') !== null;
		},
		[overlapX, overlapY] as const
	);
	expect(topElementIsInsideFrame).toBe(true);
});

test('CCC S3 wiring: strip drives the shell selection; history opens stored scope; New Chat returns to hero', async ({
	page
}) => {
	// Offline-safe scripted live bag (the reviewed live-nav pattern): the
	// readable `mai_csrf` cookie attaches the live composition, every
	// needed route is mocked, the channel is a scripted WebSocket.
	const SERVER_BETA_ID = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
	const SERVICE_ID = '3f9d2c4e-0001-4001-8001-00000000d001';
	const SESSION_ID = '55555555-5555-4555-8555-555555555555';
	const profileFor = (serverId: string, name: string) => ({
		schema_version: 1,
		server_id: serverId,
		name,
		prompt_revision: 1,
		prompt_text: 'fixture',
		environment: 'linux_ssh',
		ssh: { host: `${name}.example`, port: 22, user: 'mai', host_key: 'k', credential_ref: 'MAI_SSH_KEY' },
		privilege: 'sudo',
		capabilities: ['memory.recall'],
		revision: 1,
		sha256: 'f'.repeat(64),
		projection: { revision: 1, stale: false }
	});

	await page.context().addCookies([
		{ name: 'mai_csrf', value: 'fixture-csrf', url: 'http://127.0.0.1:4173' }
	]);
	await page.route('**/api/v1/servers', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				servers: [profileFor(SERVER_ALPHA_ID, 'Server Alpha'), profileFor(SERVER_BETA_ID, 'Server Beta')]
			})
		})
	);
	await page.route('**/api/v1/models/profiles', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ profiles: [{ schema_version: 1, name: 'glm53flash', connection: 'litellm', model: 'z-ai-coding/glm-5.3-flash' }] })
		})
	);
	await page.route('**/api/v1/models/selection', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ schema_version: 1, connection: 'litellm', profile: 'glm53flash' })
		})
	);
	// Draft @service options: Alpha adopts nginx-proxy, Beta adopts grafana
	// — the reload proof distinguishes them.
	await page.route('**/api/v1/servers/*/applications**', (route) => {
		const serverId = new URL(route.request().url()).pathname.split('/')[4];
		const name = serverId === SERVER_BETA_ID ? 'grafana' : 'nginx-proxy';
		return route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				server_id: serverId,
				applications: [
					{
						application_id: `${SERVICE_ID}-${name}`,
						server_id: serverId,
						name,
						origin: 'manual',
						revision: 1
					}
				],
				limit: 10,
				offset: 0,
				total: 1
			})
		});
	});
	// The agent-session family: list (drawer), detail (stored chips),
	// history (the transcript page).
	await page.route('**/api/v1/agent-sessions**', (route) => {
		const url = new URL(route.request().url());
		const segments = url.pathname.split('/');
		const json = (body: unknown) =>
			route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
		if (segments.length === 4) {
			// GET /api/v1/agent-sessions — the drawer's bounded first page.
			return json({
				items: [
					{
						session_id: SESSION_ID,
						server_id: SERVER_ALPHA_ID,
						source: 'interactive',
						status: 'idle',
						title: 'wiring proof',
						latest_seq: 1,
						total_entries: 1,
						compaction_count: 0,
						created_at: '2026-01-01T00:00:00Z',
						updated_at: '2026-01-01T00:00:00Z',
						service_id: SERVICE_ID,
						service_name: 'nginx-proxy',
						tag_preview: null
					}
				],
				limit: 10,
				offset: 0,
				total: 1,
				has_more: false
			});
		}
		if (segments.at(-1) === 'history') {
			return json({
				items: [
					{
						schema_version: 1,
						entry_id: 'eeeeeeee-eeee-4eee-8eee-000000000001',
						session_id: SESSION_ID,
						seq: 1,
						run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr',
						durable_source_id: 'turn:wiring',
						created_at: '2026-01-01T00:00:00Z',
						payload: {
							kind: 'user_message',
							text: 'hello from the wiring proof',
							client_request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
							source: 'interactive'
						}
					}
				],
				limit: 30,
				offset: 0,
				total: 1,
				has_more: false,
				has_older: false,
				oldest_seq: 1,
				latest_seq: 1,
				byte_size: 96,
				truncated_by_bytes: false,
				next_before_seq: null
			});
		}
		// GET /api/v1/agent-sessions/{id} — the STORED scope (SCOPE-3).
		return json({
			session_id: SESSION_ID,
			server_id: SERVER_ALPHA_ID,
			service_id: SERVICE_ID,
			service_name: 'nginx-proxy',
			source: 'interactive',
			status: 'idle',
			title: 'wiring proof',
			latest_seq: 1,
			total_entries: 1,
			compaction_count: 0,
			created_at: '2026-01-01T00:00:00Z',
			updated_at: '2026-01-01T00:00:00Z'
		});
	});
	// The session channel: scripted hello handshake + the bounded snapshot.
	await page.routeWebSocket(/\/api\/v1\/agent-sessions\/.+\/channel/, (ws) => {
		ws.onMessage((data) => {
			if (!String(data).includes('"hello"')) return;
			ws.send(
				JSON.stringify({
					type: 'hello_ok',
					protocol_version: 1,
					session_id: SESSION_ID,
					server_id: SERVER_ALPHA_ID,
					stream_id: 'wiring-s1',
					latest_seq: 1
				})
			);
			// The server's bounded truth follows the handshake: one
			// committed user turn.
			ws.send(
				JSON.stringify({
					type: 'snapshot',
					stream_id: 'wiring-s1',
					entries: [
						{
							schema_version: 1,
							entry_id: 'eeeeeeee-eeee-4eee-8eee-000000000001',
							session_id: SESSION_ID,
							seq: 1,
							run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr',
							durable_source_id: 'turn:wiring',
							created_at: '2026-01-01T00:00:00Z',
							payload: {
								kind: 'user_message',
								text: 'hello from the wiring proof',
								client_request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
								source: 'interactive'
							}
						}
					]
				})
			);
		});
	});

	await page.goto('/');
	await expect(page.getByTestId('server-tree-nav')).toBeVisible();
	const column = page.getByTestId('bottom-chat-dock');
	const panel = column.getByTestId('session-chat-panel');

	// HERO: the strip is the interactive draft scope — the server select
	// carries the shell's selection and Alpha's services load into the
	// draft @service picker.
	await expect(panel).toHaveAttribute('data-state', 'hero');
	const serverSelect = page.getByTestId('chat-scope-server-select');
	await expect(serverSelect).toHaveValue(SERVER_ALPHA_ID);
	await expect(page.getByTestId('session-service-select')).toHaveValue('');
	await expect(page.getByTestId('session-service-select')).toContainText('nginx-proxy');

	// The drawer's history row opens THAT session → ACTIVE with its OWN
	// stored scope and transcript.
	await page.getByTestId('drawer-history-item').first().click();
	await expect(panel).toHaveAttribute('data-state', 'active');
	await expect(page.getByTestId('session-header-server')).toHaveText('Server: Server Alpha');
	await expect(page.getByTestId('session-header-service')).toHaveText('Service: @nginx-proxy');
	await expect(page.getByTestId('entry-user-message')).toContainText('hello from the wiring proof');

	// A selection change while ACTIVE never rebinds the session: the strip
	// has no interactive select and the chips keep the stored scope.
	await page.getByTestId('server-select').selectOption({ label: 'Active: Server Beta' });
	await expect(page.getByTestId('session-header-server')).toHaveText('Server: Server Alpha');
	await expect(page.getByTestId('chat-scope-server-select')).toHaveCount(0);
	expect(
		await page.evaluate(() => window.localStorage.getItem('mai.workspace.selected-server.v1'))
	).toBe(SERVER_BETA_ID);

	// + New Chat returns the column to HERO: the draft scope strip, no
	// transcript, and the composer focused for the next first turn.
	await page.getByTestId('drawer-new-chat').click();
	await expect(panel).toHaveAttribute('data-state', 'hero');
	await expect(page.getByTestId('session-history-viewport')).toHaveCount(0);
	await expect(page.getByTestId('entry-user-message')).toHaveCount(0);
	// The draft scope follows the drawer selection (Beta): the strip select
	// reads Beta and the draft service options reloaded for Beta.
	await expect(serverSelect).toHaveValue(SERVER_BETA_ID);
	await expect(page.getByTestId('session-service-select')).toContainText('grafana');
	const composerFocused = await panel.evaluate(
		(el) => el.querySelector('textarea') === document.activeElement
	);
	expect(composerFocused).toBe(true);
});
