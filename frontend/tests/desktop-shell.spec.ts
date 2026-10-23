import { expect, test, type Locator, type Page } from '@playwright/test';

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

	await page.goto('/');
	await expect(page.getByTestId('server-tree-nav')).toBeVisible();

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
});
