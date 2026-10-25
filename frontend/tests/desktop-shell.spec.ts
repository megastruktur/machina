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

	// The bottom chat dock's ModelProfileSelect reacts to `live` becoming true
	// after `connectLive()` and replaces the static fixture options with ONLY
	// the configured profile (`glm53flash`).
	const dockSelect = page.getByTestId('bottom-chat-dock').getByTestId('compose-profile-select');
	await expect(dockSelect.locator('option')).toHaveText(['Select profile…', 'glm53flash']);
	await expect(dockSelect).toHaveValue('glm53flash');

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

test('the bottom chat dock is always visible, interactive offline and performs no API requests', async ({
	page
}) => {
	const urls: string[] = [];
	page.on('request', (request) => urls.push(request.url()));

	await page.goto('/');
	const dock = page.getByTestId('bottom-chat-dock');
	await expect(dock).toBeVisible();
	// The dock is NOT a registry window: no panel kind, no window frame.
	expect(await dock.getAttribute('data-panel-kind')).toBeNull();
	await expect(dock.locator('.mai-window-frame')).toHaveCount(0);
	await expect(frame(page, 'chat')).toHaveCount(0);

	// Draft context: the default Server Chip follows the launcher selection.
	await expect(dock.getByTestId('chat-server-chip')).toContainText('Server Alpha');
	await expect(dock.getByTestId('chat-server-chip-remove')).toBeVisible();

	// Offline compose round trip through the in-memory slice: the recorded
	// conversation renders and the context locks — all without any request.
	// The model profile + read-only toggle live directly in the composer bar.
	await dock.getByTestId('compose-message').fill('Dock fixture message');
	await dock.getByTestId('compose-profile').fill('fixture-fast');
	await dock.getByTestId('chat-submit').click();
	await expect(dock.getByTestId('conversation-state')).toHaveText('Running');
	await expect(dock.getByTestId('chat-server-chip')).toHaveAttribute('data-locked', 'true');
	await expect(dock.getByTestId('chat-context-locked')).toHaveCount(0);
	await expect(dock.getByTestId('chat-server-chip-remove')).toHaveCount(0);

	// `New Chat` returns to an unlocked draft with the default chip.
	await page.getByTestId('drawer-new-chat').click();
	await expect(dock.getByTestId('chat-server-chip')).toContainText('Server Alpha');
	await expect(dock.getByTestId('chat-server-chip-remove')).toBeVisible();

	// Collapse/expand keeps the dock (and its mounted panel) alive.
	await dock.getByTestId('bottom-chat-dock-toggle').click();
	await expect(dock).toBeVisible();
	await expect(dock.getByTestId('chat-root')).toBeHidden();
	await dock.getByTestId('bottom-chat-dock-toggle').click();
	await expect(dock.getByTestId('chat-root')).toBeVisible();

	expect(urls.filter((url) => url.includes('/api'))).toEqual([]);
});

for (const viewport of [{width: 1280, height: 800}, {width: 360, height: 640}]) {
	test(`real dock ${viewport.width}: composer stays reachable with both disclosures`, async ({page}) => {
		await page.setViewportSize(viewport);
		const requests: string[] = [];
		page.on('request', r => requests.push(r.url()));
		await page.goto('/');
		// Existing user-controlled shell navigation, not a geometry override.
		if (viewport.width === 360) await page.getByTestId('side-drawer-toggle').click();
		const dock = page.getByTestId('bottom-chat-dock');
		await expect(dock.getByTestId('compose-profile')).toBeVisible();
		await expect(dock.getByTestId('compose-read-only-toggle')).toBeVisible();
		await dock.getByTestId('compose-profile').fill('fixture-fast');
		await dock.getByTestId('compose-message').fill('Long fixture token '.repeat(150));
		await dock.getByTestId('chat-submit').click();
		await expect(dock.getByTestId('conversation-state')).toHaveText('Running');
		await dock.getByTestId('compose-message').fill('Preserve this dock draft');
		for (const state of ['details', 'closed']) {
			// Open rides the trigger; the expanded panel covers it, so close
			// rides the panel's own ✕ affordance.
			if (state === 'details') await dock.getByTestId('chat-details-trigger').click();
			else await dock.getByTestId('chat-details-close').click();
			for (const id of ['compose-message', 'chat-submit', 'chat-details-trigger']) {
				const box = await dock.getByTestId(id).boundingBox();
				expect(box, `${state}: ${id}`).not.toBeNull();
				expect(box!.y, `${state}: ${id} top`).toBeGreaterThanOrEqual(0);
				expect(box!.y + box!.height, `${state}: ${id} bottom`).toBeLessThanOrEqual(viewport.height);
				expect(box!.x + box!.width, `${state}: ${id} right`).toBeLessThanOrEqual(viewport.width);
			}
			expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
			const scroll = await dock.locator('.mai-chat__conversation-scroll').evaluate(el => ({h: el.clientHeight, total: el.scrollHeight}));
			expect(scroll.h).toBeGreaterThan(24);
			expect(scroll.total).toBeGreaterThan(scroll.h);
		}
		await expect(dock.getByTestId('compose-message')).toHaveValue('Preserve this dock draft');
		expect(requests.filter(url => url.includes('/api'))).toEqual([]);
	});
}

test('drawer Chat History limits to 10 chats, scrolls internally, and loads more on demand', async ({
	page
}) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/');
	const dock = page.getByTestId('bottom-chat-dock');

	// Submit 12 distinct chats in the offline dock.
	for (let i = 1; i <= 12; i++) {
		if (i > 1) {
			await page.getByTestId('drawer-new-chat').click();
		}
		await dock.getByTestId('compose-message').fill(`Chat session ${i}`);
		await dock.getByTestId('chat-submit').click();
		await expect(dock.getByTestId('conversation-state')).toHaveText('Running');
	}

	const drawerSection = page.getByTestId('drawer-chat-section');
	await expect(drawerSection.getByTestId('drawer-history-item')).toHaveCount(10);
	const list = drawerSection.getByTestId('drawer-history-list');
	await expect(list).toBeVisible();
	const overflowY = await list.evaluate((el) => getComputedStyle(el).overflowY);
	expect(overflowY).toBe('auto');

	const loadMore = drawerSection.getByTestId('drawer-history-load-more');
	await expect(loadMore).toBeVisible();
	await expect(loadMore).toContainText('Load more (10 of 12)');

	await loadMore.click();
	await expect(drawerSection.getByTestId('drawer-history-item')).toHaveCount(12);
	await expect(drawerSection.getByTestId('drawer-history-load-more')).toHaveCount(0);

	// Rename the top chat session directly in the left drawer.
	await drawerSection.getByTestId('drawer-history-rename').first().click({ force: true });
	const renameInput = drawerSection.getByTestId('drawer-history-rename-input');
	await expect(renameInput).toBeVisible();
	await renameInput.fill('Production Incident Investigation');
	await drawerSection.getByTestId('drawer-history-rename-save').click();
	await expect(drawerSection.getByTestId('drawer-history-item').first()).toContainText(
		'Production Incident Investigation'
	);
});

test('opened windows stack ABOVE the bottom chat dock and unauthorized live session redirects to /login', async ({
	page
}) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/');

	const dock = page.getByTestId('bottom-chat-dock');
	await openPanel(page, NOTES);
	const notes = frame(page, NOTES);

	// The desktop canvas (`z-index: 20`) sits above the bottom chat dock
	// (`z-index: 5`), so any opened window renders on top of the chat dock,
	// while empty canvas space has `pointer-events: none` so the dock stays
	// interactive underneath.
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

	// Drag the window directly over the bottom chat dock and verify hit-testing
	// lands inside the window frame rather than being covered by the chat dock.
	const dockBox = await dock.boundingBox();
	const bar = notes.locator('.mai-window-frame__titlebar');
	const barBox = await bar.boundingBox();
	expect(dockBox).not.toBeNull();
	expect(barBox).not.toBeNull();

	await page.mouse.move(barBox!.x + barBox!.width / 2, barBox!.y + barBox!.height / 2);
	await page.mouse.down();
	await page.mouse.move(dockBox!.x + dockBox!.width / 2, dockBox!.y + 20, { steps: 5 });
	await page.mouse.up();

	const updatedBox = await notes.boundingBox();
	expect(updatedBox).not.toBeNull();
	const overlapX = Math.max(updatedBox!.x, dockBox!.x) + 40;
	const overlapY = Math.max(updatedBox!.y, dockBox!.y) + 20;
	const topElementIsInsideFrame = await page.evaluate(
		([x, y]) => {
			const hit = document.elementFromPoint(x, y);
			return hit !== null && hit.closest('.mai-window-frame') !== null;
		},
		[overlapX, overlapY] as const
	);
	expect(topElementIsInsideFrame).toBe(true);
});

// ---------------------------------------------------------------------------
// 24a D-1 — the COMPOSED live dock journey V6 proved broken: real shell +
// real ChatPanel + real browser client routes over mocked HTTP. The dock
// boots pinned to ONE real server; removing the chip must re-scope the
// whole canonical surface (history list, create, event tail, messages,
// restore) to the Global sentinel — the drawer must show the created
// conversation and a restore/reload must never 404 or ride the old server.
// ---------------------------------------------------------------------------

const LIVE_SERVER_ID = 'af130194-930d-45da-a47c-0aa22bb14d46';
const GLOBAL_SENTINEL = '00000000-0000-0000-0000-000000000000';
const PRE_GLOBAL_ID = '9ce236c1-a253-48b9-9eaa-350016223e2f';
const CREATED_ID = '4ea2eca6-d7f0-415d-a131-ce0d1742f51a';
const CREATED_RUN = '10bc17bc-6211-4a8a-b48e-b490d9b5284b';
/** The §8 task episode riding the created conversation (24b D-2). */
const CREATED_TASK = '71a5c9d8-3e2f-4b6a-9c4d-1f2e3a4b5c6d';
const TASK_STEP_A = 'inventory-dependent-jobs';
const TASK_STEP_B = 'disable-and-remove-unit';
const TASK_EVIDENCE = '9f8e7d6c-5a4b-4c3d-8e2f-1a2b3c4d5e6f';
const CSRF_COOKIE = 'mai_csrf';

interface MockMessage {
	message_id: string;
	sequence: number;
	role: 'user' | 'assistant';
	content: string;
	created_at: string;
}

interface MockConversation {
	serverId: string;
	createdAt: string;
	updatedAt: string;
	activeRunId: string | null;
	/** The conversation's ONE §8 task episode (24b D-2), else null. */
	activeTaskId: string | null;
	messages: MockMessage[];
}

/** Build one canonical request record for the scope audit. */
interface CanonicalRequest {
	path: string;
	method: string;
	serverId: string | null;
	status: number;
	/** Shell epoch: 0 = first boot, 1 = after the reload (a fresh draft). */
	epoch: number;
}

test('live dock Global journey (24a D-1): unpin → Global history → submit → drawer shows it → restore/reload stays on the sentinel', async ({
	page,
	context
}) => {
	await page.setViewportSize({ width: 1280, height: 800 });

	// The readable CSRF cookie is the honest live-attach probe (auth ADR §5).
	await context.addCookies([
		{ name: CSRF_COOKIE, value: 'playwright-csrf', url: 'http://127.0.0.1:4173' }
	]);

	// --- the canonical store the routes serve --------------------------------
	const now = '2026-10-25T15:13:16Z';
	const conversations = new Map<string, MockConversation>();
	const addMessages = (id: string, messages: MockMessage[]): void => {
		const row = conversations.get(id);
		if (row) {
			row.messages = messages;
			row.updatedAt = messages[messages.length - 1]?.created_at ?? row.updatedAt;
		}
	};
	conversations.set(PRE_GLOBAL_ID, {
		serverId: GLOBAL_SENTINEL,
		createdAt: '2026-10-25T15:00:00Z',
		updatedAt: '2026-10-25T15:05:00Z',
		activeRunId: null,
		activeTaskId: null,
		messages: [
			{
				message_id: 'aa000000-0000-4000-8000-000000000001',
				sequence: 1,
				role: 'user',
				content: 'Pre-existing consult question.',
				created_at: '2026-10-25T15:00:10Z'
			},
			{
				message_id: 'aa000000-0000-4000-8000-000000000002',
				sequence: 2,
				role: 'assistant',
				content: 'Pre-existing consult answer.',
				created_at: '2026-10-25T15:05:00Z'
			}
		]
	});
	conversations.set(CREATED_ID, {
		serverId: GLOBAL_SENTINEL,
		createdAt: now,
		updatedAt: now,
		activeRunId: null,
		// The plan-only V6 episode: a task with steps/constraints but NO
		// outcome document and ZERO evidence rows (honest empty state).
		activeTaskId: CREATED_TASK,
		messages: []
	});

	const canonicalRequests: CanonicalRequest[] = [];
	const createdBodies: Array<Record<string, unknown>> = [];
	// Shell epoch: the reload boots a FRESH panel whose pinned-scope draft
	// correctly lists the real server again before the test unpins it — the
	// audit runs per epoch (the "read scope follows the CURRENT draft" rule
	// applies within one shell life, never across reloads).
	let epoch = 0;
	const jsonHeaders = { 'content-type': 'application/json' };
	const fulfillJson = (route: Route, status: number, body: unknown) =>
		route.fulfill({ status, headers: jsonHeaders, body: JSON.stringify(body) });
	const summaryOf = (id: string): Record<string, unknown> => {
		const row = conversations.get(id)!;
		return {
			conversation_id: id,
			server_id: row.serverId,
			title: null,
			created_at: row.createdAt,
			updated_at: row.updatedAt,
			message_count: row.messages.length,
			active_task_id: row.activeTaskId,
			active_run_id: row.activeRunId
		};
	};
	const detailOf = (id: string): Record<string, unknown> => ({
		...summaryOf(id),
		history_completeness: 'complete',
		missing_range: null,
		legacy_gaps: []
	});
	/** The §8 compact header of the created episode (D-2; plan-only V6 shape). */
	const taskHeaderOf = (conversationId: string): Record<string, unknown> => ({
		task_id: CREATED_TASK,
		conversation_id: conversationId,
		server_id: GLOBAL_SENTINEL,
		revision: 3,
		title: 'Decommission the legacy cron job',
		summary:
			'Understood: disable the nightly cron on megaserver, confirm nothing depends on it, then remove the unit.',
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
		created_at: now,
		updated_at: now
	});
	const notFound = (
		route: Route,
		request: { serverId: string | null; path: string; method: string }
	) => {
		canonicalRequests.push({ ...request, status: 404, epoch });
		return fulfillJson(route, 404, {
			detail: { code: 'not_found', message: 'no conversation in the requesting server scope' }
		});
	};

	// ONE dispatcher for the whole canonical surface: list/create/detail/
	// messages/events, each scope-checked exactly like the backend (C8).
	await page.route('**/api/v1/chat/conversations**', async (route) => {
		const request = route.request();
		const url = new URL(request.url());
		const serverId = url.searchParams.get('server_id');
		const parts = url.pathname.split('/').filter((part) => part !== '');
		const record = (status: number, overrideServerId?: string | null): CanonicalRequest => {
			const entry = {
				path: url.pathname + url.search,
				method: request.method(),
				serverId: overrideServerId !== undefined ? overrideServerId : serverId,
				status,
				epoch
			};
			canonicalRequests.push(entry);
			return entry;
		};
		// POST /api/v1/chat/conversations — create + first turn.
		if (request.method() === 'POST' && parts.length === 4) {
			const body = request.postDataJSON() as Record<string, unknown>;
			createdBodies.push(body);
			record(201, typeof body.server_id === 'string' ? body.server_id : null);
			addMessages(CREATED_ID, [
				{
					message_id: 'bb000000-0000-4000-8000-000000000001',
					sequence: 1,
					role: 'user',
					content: String(body.message),
					created_at: now
				},
				{
					message_id: 'bb000000-0000-4000-8000-000000000002',
					sequence: 2,
					role: 'assistant',
					content: 'Recorded dock reply.',
					created_at: now
				}
			]);
			const created = conversations.get(CREATED_ID)!;
			created.activeRunId = CREATED_RUN;
			return fulfillJson(route, 201, {
				conversation_id: CREATED_ID,
				message_id: 'bb000000-0000-4000-8000-000000000001',
				task_id: null,
				run_id: CREATED_RUN,
				revision: 1,
				disposition: 'created'
			});
		}
		const conversationId = parts[4] !== undefined ? decodeURIComponent(parts[4]) : null;
		const tail = parts[5] ?? null;
		if (conversationId === null) {
			// GET /api/v1/chat/conversations?server_id=… — bounded page.
			record(200);
			const items = [...conversations.entries()]
				.filter(([, row]) => row.serverId === serverId)
				.sort(([, a], [, b]) => b.updatedAt.localeCompare(a.updatedAt))
				.map(([id]) => summaryOf(id));
			return fulfillJson(route, 200, {
				limit: Number(url.searchParams.get('limit') ?? 10),
				offset: Number(url.searchParams.get('offset') ?? 0),
				total: items.length,
				has_more: false,
				items
			});
		}
		const row = conversations.get(conversationId);
		if (row === undefined || row.serverId !== serverId)
			return notFound(route, { serverId, path: url.pathname, method: request.method() });
		if (tail === 'messages') {
			record(200);
			return fulfillJson(route, 200, {
				limit: Number(url.searchParams.get('limit') ?? 10),
				offset: Number(url.searchParams.get('offset') ?? 0),
				total: row.messages.length,
				has_more: false,
				conversation_id: conversationId,
				history_completeness: 'complete',
				items: row.messages
			});
		}
		if (tail === 'events') {
			record(200);
			const afterSeq = Number(url.searchParams.get('after_seq') ?? 0);
			const events = row.messages
				.filter((message) => message.sequence > afterSeq)
				.map((message) => ({
					seq: message.sequence,
					kind: 'transcript.appended',
					message_id: message.message_id,
					role: message.role,
					source_kind: message.role === 'user' ? 'api' : 'system',
					redacted: false,
					has_content_ref: false,
					created_at: message.created_at
				}));
			return fulfillJson(route, 200, {
				conversation_id: conversationId,
				limit: 50,
				offset: 0,
				total: events.length,
				has_more: false,
				after_seq: afterSeq,
				events,
				current: {
					latest_seq: row.messages.length,
					history_completeness: 'complete',
					active_run_id: row.activeRunId,
					active_task_id: row.activeTaskId,
					pending_steering: 0
				}
			});
		}
		if (tail === 'task') {
			// GET /api/v1/chat/conversations/{id}/task — the ONE active episode
			// header, scope-checked exactly like the backend: no episode in
			// this server scope collapses into the SAME scope-blind 404.
			if (row.activeTaskId === null) {
				record(404);
				return fulfillJson(route, 404, {
					detail: {
						code: 'task_not_found',
						message: 'no active task episode in the requesting server scope'
					}
				});
			}
			record(200);
			return fulfillJson(route, 200, taskHeaderOf(conversationId));
		}
		// GET /api/v1/chat/conversations/{id} — header + gap honesty.
		record(200);
		return fulfillJson(route, 200, detailOf(conversationId));
	});

	// The lease-holding run facade + its SSE route (the producer pair).
	await page.route('**/api/v1/chat/runs/**', async (route) => {
		const url = new URL(route.request().url());
		const parts = url.pathname.split('/').filter((part) => part !== '');
		const runId = parts[4] !== undefined ? decodeURIComponent(parts[4]) : '';
		if (runId === '') {
			return route.fulfill({
				status: 404,
				headers: jsonHeaders,
				body: JSON.stringify({ detail: { code: 'not_found', message: 'no run id' } })
			});
		}
		if (parts[5] === 'events') {
			// Bounded SSE stream: closes immediately with no frames — the
			// subscription lifecycle is exercised, nothing rides it here.
			return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
		}
		const row = [...conversations.values()].find((one) => one.activeRunId === runId);
		if (row === undefined) {
			return route.fulfill({
				status: 404,
				headers: jsonHeaders,
				body: JSON.stringify({ detail: { code: 'not_found', message: 'no such run' } })
			});
		}
		return route.fulfill({
			status: 200,
			headers: jsonHeaders,
			body: JSON.stringify({
				run_id: runId,
				server_id: row.serverId,
				parent_run_id: null,
				depth: 0,
				profile_revision: 1,
				prompt_revision: 1,
				model_profile: 'glm53flash',
				requested_grants: [],
				state: 'running',
				cancel_requested_at: null,
				submitted_at: row.createdAt,
				updated_at: row.updatedAt
			})
		});
	});

	// The §8 task collections of the created episode (24b D-2): bounded,
	// scope-checked pages. The evidence page is HONESTLY empty (total 0 —
	// the V6 plan-only episode produced no outcome document and zero
	// evidence rows, which must render as absence, never fabrication).
	const taskRequests: Array<{ path: string; serverId: string | null; status: number }> = [];
	await page.route('**/api/v1/chat/tasks/**', async (route) => {
		const url = new URL(route.request().url());
		const parts = url.pathname.split('/').filter((part) => part !== '');
		const taskId = parts[4] !== undefined ? decodeURIComponent(parts[4]) : '';
		const tail = parts[5] ?? '';
		const serverId = url.searchParams.get('server_id');
		const entry = { path: url.pathname + url.search, serverId, status: 200 };
		taskRequests.push(entry);
		if (taskId !== CREATED_TASK || serverId !== GLOBAL_SENTINEL) {
			entry.status = 404;
			return fulfillJson(route, 404, {
				detail: {
					code: 'task_not_found',
					message: 'no such task in the requesting server scope'
				}
			});
		}
		const pageOf = (items: unknown[]) => ({
			task_id: taskId,
			limit: Number(url.searchParams.get('limit') ?? 10),
			offset: Number(url.searchParams.get('offset') ?? 0),
			total: items.length,
			has_more: false,
			items
		});
		if (tail === 'items') {
			return fulfillJson(
				route,
				200,
				pageOf([
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
						evidence_refs: [{ kind: 'observation', evidence_id: TASK_EVIDENCE, note: null }],
						depends_on: [],
						superseded_at: null,
						superseded_by: null,
						created_at: now,
						updated_at: now
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
						created_at: now,
						updated_at: now
					}
				])
			);
		}
		if (tail === 'constraints') {
			return fulfillJson(
				route,
				200,
				pageOf([
					{
						task_id: taskId,
						constraint_id: 'no-scheduler-restart',
						category: 'safety',
						statement:
							'Never restart the production scheduler while the legacy unit is disabled.',
						origin: 'user',
						source_message_refs: ['bb000000-0000-4000-8000-000000000001'],
						position: 1,
						superseded_at: null,
						superseded_by: null,
						created_at: now,
						updated_at: now
					}
				])
			);
		}
		if (tail === 'revisions') {
			return fulfillJson(
				route,
				200,
				pageOf([
					{
						task_id: taskId,
						revision: 1,
						kind: 'create',
						author: 'model',
						source_message_id: null,
						item_id: null,
						payload: null,
						applied_at: now
					}
				])
			);
		}
		if (tail === 'evidence') {
			// Zero rows: the explicit empty state is the honest render.
			return fulfillJson(route, 200, pageOf([]));
		}
		entry.status = 404;
		return fulfillJson(route, 404, {
			detail: { code: 'not_found', message: 'unknown task collection' }
		});
	});

	// Live attach: one real server profile + the model profile the dock's
	// live select renders.
	await page.route('**/api/v1/servers', (route) =>
		fulfillJson(route, 200, {
			servers: [
				{
					schema_version: 1,
					server_id: LIVE_SERVER_ID,
					name: 'megaserver',
					prompt_revision: 1,
					prompt_text: 'fixture',
					environment: 'linux_ssh',
					ssh: {
						host: 'megaserver',
						port: 22,
						user: 'mai',
						host_key: '',
						credential_ref: 'fixture-credential'
					},
					privilege: 'sudo',
					capabilities: [],
					revision: 1,
					sha256: 'ab'.repeat(32),
					projection: { revision: 1, stale: false }
				}
			]
		})
	);
	await page.route('**/api/v1/models/profiles', (route) =>
		fulfillJson(route, 200, {
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
		fulfillJson(route, 200, { schema_version: 1, connection: 'litellm', profile: 'glm53flash' })
	);

	const dock = page.getByTestId('bottom-chat-dock');
	const drawer = page.getByTestId('drawer-chat-section');

	// Boot pinned to the REAL server (the V6 starting state).
	await page.goto('/');
	await expect(page.getByTestId('server-tree-nav')).toContainText('megaserver');
	await expect(dock.getByTestId('chat-server-chip')).toContainText('megaserver');
	// The pinned-scope drawer: no LIVE conversations recorded.
	await drawer.getByTestId('drawer-history-toggle').click();
	await drawer.getByTestId('drawer-history-toggle').click();
	await expect(drawer.getByTestId('drawer-history-empty')).toBeVisible();
	const pinnedLists = canonicalRequests.filter((one) => one.method === 'GET');
	expect(pinnedLists.length).toBeGreaterThanOrEqual(1);
	for (const one of pinnedLists) expect(one.serverId).toBe(LIVE_SERVER_ID);

	// --- the V6 defect trigger: unpin the chip ------------------------------
	await dock.getByTestId('chat-server-chip-remove').click();
	await expect(dock.getByTestId('chat-general-consult-badge')).toBeVisible();

	// Drawer refresh after the unpin: the history list now rides the
	// sentinel and shows the PRE-EXISTING Global conversation.
	await drawer.getByTestId('drawer-history-toggle').click();
	await drawer.getByTestId('drawer-history-toggle').click();
	await expect(
		drawer.getByTestId('drawer-history-item').filter({ hasText: PRE_GLOBAL_ID.slice(0, 8) })
	).toHaveCount(1);

	// Submit a NEW conversation from the dock: the create body names the
	// Global sentinel — never the previously selected server.
	await dock.getByTestId('compose-message').fill('Global consult from composed dock');
	await dock.getByTestId('chat-submit').click();
	await expect(
		dock.getByTestId('canonical-user-message').filter({ hasText: 'Global consult from composed dock' })
	).toHaveCount(1);
	await expect(dock.getByTestId('canonical-assistant-message')).toContainText(
		'Recorded dock reply.'
	);
	await expect(dock.getByTestId('canonical-problem')).toHaveCount(0);
	expect(createdBodies).toHaveLength(1);
	expect(createdBodies[0].server_id).toBe(GLOBAL_SENTINEL);
	expect(createdBodies[0].message).toBe('Global consult from composed dock');

	// The created conversation appears in the drawer history next to the
	// pre-existing one (V6 saw "Chat History 0" forever).
	await expect(
		drawer.getByTestId('drawer-history-item').filter({ hasText: CREATED_ID.slice(0, 8) })
	).toHaveCount(1);

	// --- restore: open the created conversation from the drawer -------------
	await drawer
		.getByTestId('drawer-history-item')
		.filter({ hasText: CREATED_ID.slice(0, 8) })
		.click();
	await expect(dock.getByTestId('canonical-user-message').filter({ hasText: 'Global consult from composed dock' })).toBeVisible();
	await expect(dock.getByTestId('canonical-assistant-message')).toContainText('Recorded dock reply.');
	await expect(dock.getByTestId('canonical-problem')).toHaveCount(0);

	// --- 24b D-2: the dock's bound task services mount the §8 blocks --------
	// TaskUnderstanding renders the REAL episode (scope, summary, constraints,
	// steps); TaskPlan renders the real steps and the honest evidence-empty
	// state. The plan-only episode has NO outcome document: TaskEvidence
	// stays absent — absence is the honesty proof, nothing is fabricated.
	await expect(dock.getByTestId('task-understanding')).toHaveAttribute('data-state', 'ready');
	await expect(dock.getByTestId('task-understanding-scope-title')).toHaveText(
		'Decommission the legacy cron job'
	);
	await expect(dock.getByTestId('task-understanding-scope-summary')).toContainText('Understood:');
	await expect(dock.getByTestId('task-understanding-constraint-row')).toHaveCount(1);
	await expect(dock.getByTestId('task-understanding-step-row')).toHaveCount(2);
	await expect(dock.getByTestId('task-plan')).toHaveAttribute('data-state', 'ready');
	await expect(dock.getByTestId('task-plan-title')).toHaveText(
		'Decommission the legacy cron job'
	);
	await expect(dock.getByTestId('task-plan-step-row')).toHaveCount(2);
	await expect(dock.getByTestId('task-plan-evidence-empty')).toHaveText(
		'No evidence records linked.'
	);
	await expect(dock.getByTestId('task-plan-revision-row')).toHaveCount(1);
	await expect(dock.getByTestId('task-outcome')).toHaveCount(0);

	// --- reload: a fresh shell restores the same Global conversation --------
	epoch = 1;
	await page.reload();
	await expect(page.getByTestId('server-tree-nav')).toContainText('megaserver');
	await dock.getByTestId('chat-server-chip-remove').click();
	await expect(dock.getByTestId('chat-general-consult-badge')).toBeVisible();
	await drawer.getByTestId('drawer-history-toggle').click();
	await drawer.getByTestId('drawer-history-toggle').click();
	await expect(
		drawer.getByTestId('drawer-history-item').filter({ hasText: CREATED_ID.slice(0, 8) })
	).toHaveCount(1);
	await drawer
		.getByTestId('drawer-history-item')
		.filter({ hasText: CREATED_ID.slice(0, 8) })
		.click();
	await expect(
		dock.getByTestId('canonical-user-message').filter({ hasText: 'Global consult from composed dock' })
	).toHaveCount(1);
	await expect(dock.getByTestId('canonical-problem')).toHaveCount(0);

	// --- the scope audit -----------------------------------------------------
	// 1. The create rode the sentinel (asserted above). 2. Within each shell
	// epoch, NO canonical request regressed to the previously selected server
	// once the first sentinel-scoped request happened — a fresh reload boot
	// correctly re-lists the pinned draft scope until the test unpins again —
	// and 3. nothing 404'd (a scope-blind events poll 404ing forever was the
	// V6 defect).
	for (const auditEpoch of [0, 1]) {
		const requests = canonicalRequests.filter((one) => one.epoch === auditEpoch);
		expect(requests.length, `epoch ${auditEpoch} recorded`).toBeGreaterThan(0);
		const firstSentinelIndex = requests.findIndex((one) => one.serverId === GLOBAL_SENTINEL);
		expect(firstSentinelIndex, `epoch ${auditEpoch} sentinel seen`).toBeGreaterThan(-1);
		for (const [index, one] of requests.entries()) {
			if (index < firstSentinelIndex) {
				expect(one.serverId, `epoch ${auditEpoch} pre-unpin ${one.path}`).toBe(LIVE_SERVER_ID);
			} else {
				expect(one.serverId, `epoch ${auditEpoch} post-unpin ${one.path}`).toBe(GLOBAL_SENTINEL);
			}
			expect(one.status, one.path).toBeLessThan(400);
		}
	}
	// The event tail actually polled the created conversation — on the sentinel.
	const eventReads = canonicalRequests.filter(
		(one) => one.path.includes(`/${CREATED_ID}/events`) && one.method === 'GET'
	);
	expect(eventReads.length).toBeGreaterThanOrEqual(1);
	for (const one of eventReads) {
		expect(one.serverId).toBe(GLOBAL_SENTINEL);
		expect(one.status).toBe(200);
	}

	// --- the pocket leg (24b): the same restore at 360×640 -------------------
	// Understanding must VISIBLY render real scope/summary/constraints/steps
	// at the V6 viewport — composition proof, not a unit fixture.
	epoch = 2;
	await page.setViewportSize({ width: 360, height: 640 });
	await page.reload();
	await expect(page.getByTestId('server-tree-nav')).toContainText('megaserver');
	await dock.getByTestId('chat-server-chip-remove').click();
	await expect(dock.getByTestId('chat-general-consult-badge')).toBeVisible();
	await drawer.getByTestId('drawer-history-toggle').click();
	await drawer.getByTestId('drawer-history-toggle').click();
	await expect(
		drawer.getByTestId('drawer-history-item').filter({ hasText: CREATED_ID.slice(0, 8) })
	).toHaveCount(1);
	await drawer
		.getByTestId('drawer-history-item')
		.filter({ hasText: CREATED_ID.slice(0, 8) })
		.click();
	await expect(dock.getByTestId('task-understanding')).toHaveAttribute('data-state', 'ready');
	await expect(dock.getByTestId('task-understanding-scope-title')).toHaveText(
		'Decommission the legacy cron job'
	);
	await expect(dock.getByTestId('task-understanding-constraint-row')).toHaveCount(1);
	await expect(dock.getByTestId('task-understanding-step-row')).toHaveCount(2);
	await expect(dock.getByTestId('task-plan')).toHaveAttribute('data-state', 'ready');
	await expect(dock.getByTestId('task-plan-evidence-empty')).toBeVisible();
	const scopeBox = await dock.getByTestId('task-understanding-scope').boundingBox();
	expect(scopeBox, 'understanding scope box').not.toBeNull();
	expect(scopeBox!.x).toBeGreaterThanOrEqual(0);
	expect(scopeBox!.x + scopeBox!.width).toBeLessThanOrEqual(360);
	await expect(dock.getByTestId('canonical-problem')).toHaveCount(0);
	// The pocket epoch obeys the same scope rule: post-unpin every canonical
	// read rides the sentinel and nothing 404s.
	const pocket = canonicalRequests.filter((one) => one.epoch === 2);
	expect(pocket.length, 'epoch 2 recorded').toBeGreaterThan(0);
	const pocketSentinel = pocket.findIndex((one) => one.serverId === GLOBAL_SENTINEL);
	expect(pocketSentinel, 'epoch 2 sentinel seen').toBeGreaterThan(-1);
	for (const [index, one] of pocket.entries()) {
		if (index < pocketSentinel) {
			expect(one.serverId, `epoch 2 pre-unpin ${one.path}`).toBe(LIVE_SERVER_ID);
		} else {
			expect(one.serverId, `epoch 2 post-unpin ${one.path}`).toBe(GLOBAL_SENTINEL);
		}
		expect(one.status, one.path).toBeLessThan(400);
	}

	// --- the §8 task-read audit ----------------------------------------------
	// Pre-repair the dock's services bag carried NO task methods: both blocks
	// rendered "unbound" and these requests never left the page. Every task
	// read must ride the recorded Global sentinel scope and succeed.
	expect(taskRequests.length, 'task reads recorded').toBeGreaterThanOrEqual(4);
	for (const one of taskRequests) {
		expect(one.serverId, one.path).toBe(GLOBAL_SENTINEL);
		expect(one.status, one.path).toBeLessThan(400);
	}
	for (const tail of ['items', 'constraints', 'evidence', 'revisions']) {
		expect(
			taskRequests.some((one) => one.path.includes(`/tasks/${CREATED_TASK}/${tail}`)),
			`task ${tail} read`
		).toBe(true);
	}
});
