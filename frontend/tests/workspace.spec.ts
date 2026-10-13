import { expect, test, type Locator, type Page } from '@playwright/test';

/**
 * 01-workspace-runtime — real-browser proofs for the COMPOSED desktop
 * runtime on the ACTUAL app route (frontend/src/routes/+page.svelte
 * rendering DesktopShell): the production build the webServer serves is the
 * exercised entrypoint; no backend exists and none is needed (C11: window
 * lifecycle is backend-independent, NT6).
 *
 * This card composes the reviewed adapters (drag/resize/minimize/maximize/
 * taskbar/layout/viewport) and owns the runtime wiring scenarios:
 *  - PT4 taskbar: minimize hides view-only (geometry/scope/identity/z
 *    untouched, content unmounted), the entry keeps `aria-pressed` and the
 *    scoped accessible name, restore remounts fresh content and focuses;
 *    minimizing the focused window moves focus to the topmost remaining
 *    visible window; all-hidden leaves no focused frame;
 *  - keyboard: chrome controls and taskbar entries are reachable and
 *    operable (ADR §6; maximize never moves focus, view-only);
 *  - maximize overlay (owned by this composition per the presentation
 *    card's recorded limit): fills the desktop region, drops the resize
 *    handles, suppresses geometry commits while not `normal`, restores the
 *    committed geometry;
 *  - PT6 persistence: reload restores kinds, pinned scopes, committed
 *    geometry, stacking and stored presentations with fresh view ids; a
 *    restore notice is surfaced;
 *  - NT4 negative control: a hostile stored document is discarded with a
 *    notice-safe reason, the stored copy is cleared, and the desktop stays
 *    fully usable;
 *  - NT3 viewport reflow: shrinking the viewport re-clamps live windows to
 *    the ≥64×32 visible title-bar invariant with the minimum-size floor;
 *    growing back is a no-op (no spurious commits);
 *  - error lifecycle: the deterministic broken fixture loader surfaces the
 *    safe error view with Retry, never leaks raw error text, cannot affect
 *    sibling windows and leaves the frame close usable; zero unhandled page
 *    errors.
 *
 * Focused-slice scope (brief): multiwindow focus/drag/close happy paths on
 * the same route are proven by desktop-shell.spec.ts and not repeated here;
 * PT5 close is re-proven on the broken-panel window. Known limits (recorded
 * in card evidence): keyboard Move/Resize a11y modes are the phase-gate a11y
 * work; cross-tab layout sync is last-writer-wins (ADR §5 rule 6). Pointer
 * clicks observe real window stacking — a window above another intercepts
 * its pointer events, so scenarios order their opens accordingly.
 */

const NOTES = 'fixture-notes';
const PREFS = 'fixture-prefs';
const BROKEN = 'fixture-broken';
const SERVER_ALPHA_ID = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const LAYOUT_KEY = 'mai.workspace.layout.v1';

/** A window frame locator for one panel kind (frames carry role="dialog"). */
function frame(page: Page, kind: string): Locator {
	return page.locator(`.mai-window-frame[data-panel-kind="${kind}"]`);
}

async function openPanel(page: Page, kind: string): Promise<void> {
	await page.getByTestId(`open-${kind}`).click();
	await expect(frame(page, kind)).toHaveCount(1);
}

/** The taskbar entry of one live window (addressed by its view id). */
function taskbarEntry(page: Page, windowId: string): Locator {
	return page.locator(`.mai-window-switcher__entry[data-window-id="${windowId}"]`);
}

/** Committed inline geometry of a frame (the descriptor's live projection). */
async function inlineGeometry(
	page: Page,
	kind: string
): Promise<{ x: string; y: string; width: string; height: string }> {
	return frame(page, kind).evaluate((el) => ({
		x: el.style.left,
		y: el.style.top,
		width: el.style.width,
		height: el.style.height
	}));
}

/** PT1 title-bar drag by pointer through the composed shell. */
async function dragTitleBar(page: Page, bar: Locator, dx: number, dy: number): Promise<void> {
	const box = await bar.boundingBox();
	expect(box).not.toBeNull();
	await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
	await page.mouse.down();
	await page.mouse.move(box!.x + box!.width / 2 + dx, box!.y + box!.height / 2 + dy, {
		steps: 5
	});
	await page.mouse.up();
}

/**
 * Walks REAL Tab presses until `target` has focus (bounded; no programmatic
 * focus() — the keyboard path is the behavior under test).
 */
async function tabUntil(target: Locator, maxHops: number): Promise<void> {
	const page = target.page();
	for (let hop = 0; hop < maxHops; hop += 1) {
		if (await target.evaluate((el) => document.activeElement === el)) return;
		await page.keyboard.press('Tab');
	}
	await expect(target).toBeFocused();
}

test('taskbar minimize hides view-only; restore remounts fresh content and focuses (PT4)', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	await openPanel(page, PREFS);
	const notes = frame(page, NOTES);
	const prefs = frame(page, PREFS);
	const notesId = (await notes.getAttribute('data-window-id'))!;

	// Bump the notes counter (the click also RAISES notes — PT2 activation).
	await notes.getByTestId('fixture-interactions').click();
	await expect(notes.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 1');
	await expect(notes).toHaveAttribute('data-focused', 'true');
	// A focusin on the prefs title raises prefs again (PT2 keyboard half).
	await prefs.getByRole('heading').click();
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await expect(notes).toHaveAttribute('data-focused', 'false');

	const geometryBefore = await inlineGeometry(page, NOTES);

	// Minimize the NON-focused window: focus must not move (view-only).
	await notes.getByRole('button', { name: 'Minimize' }).click();
	await expect(frame(page, NOTES)).toHaveCount(0);
	await expect(prefs).toHaveAttribute('data-focused', 'true');

	// The entry persists with hidden state and the scoped accessible name.
	const entry = taskbarEntry(page, notesId);
	await expect(entry).toHaveAttribute('aria-pressed', 'true');
	await expect(entry).toHaveAccessibleName('Notes — Server Alpha');

	// Restore via the taskbar: same view identity, committed geometry kept,
	// content remounts fresh (keep_mounted_when_hidden: false composition).
	await entry.click();
	const restored = frame(page, NOTES);
	await expect(restored).toHaveCount(1);
	await expect(restored).toHaveAttribute('data-window-id', notesId);
	await expect(restored).toHaveAttribute('data-focused', 'true');
	await expect(entry).toHaveAttribute('aria-pressed', 'false');
	await expect.poll(() => inlineGeometry(page, NOTES)).toEqual(geometryBefore);
	await expect(restored.getByTestId('fixture-interactions')).toHaveText('Fixture interactions: 0');
});

test('minimizing the focused window hands focus to the topmost visible window', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	await openPanel(page, PREFS);
	const notes = frame(page, NOTES);
	const prefs = frame(page, PREFS);

	// prefs is focused (opened last); minimizing it falls to notes (PT2).
	await prefs.getByRole('button', { name: 'Minimize' }).click();
	await expect(frame(page, PREFS)).toHaveCount(0);
	await expect(notes).toHaveAttribute('data-focused', 'true');

	// Hiding the last visible window leaves NO focused frame.
	await notes.getByRole('button', { name: 'Minimize' }).click();
	await expect(frame(page, NOTES)).toHaveCount(0);
	await expect(page.locator('.mai-window-frame[data-focused="true"]')).toHaveCount(0);

	// Both entries remain pressed; the stack's bottom entry (prefs) restores
	// and focuses its own window when activated.
	const entries = page.locator('.mai-window-switcher__entry');
	await expect(entries).toHaveCount(2);
	await expect(entries.first()).toHaveAttribute('aria-pressed', 'true');
	await expect(entries.last()).toHaveAttribute('aria-pressed', 'true');
	await entries.first().click();
	await expect(frame(page, PREFS)).toHaveAttribute('data-focused', 'true');
});

test('taskbar and chrome controls are keyboard reachable and operable (ADR §6)', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	await openPanel(page, PREFS);
	const notes = frame(page, NOTES);
	const prefs = frame(page, PREFS);
	const notesId = (await notes.getAttribute('data-window-id'))!;

	// Keyboard minimize through the chrome: real Tab walk to the notes title
	// (select → two launcher buttons → notes title) and its Minimize control.
	await tabUntil(notes.getByRole('heading'), 8);
	await tabUntil(notes.getByRole('button', { name: 'Minimize' }), 4);
	await page.keyboard.press('Enter');
	await expect(frame(page, NOTES)).toHaveCount(0);
	await expect(prefs).toHaveAttribute('data-focused', 'true');

	// Keyboard taskbar activation. Recorded headless-Chromium quirk (see
	// desktop-shell.spec.ts): after a focused control is removed from the
	// DOM the sequential-navigation start point is dropped, so Tab arrival
	// at the entry is unreliable — .focus() stands in for the arrival while
	// Enter stays the real keyboard activation.
	await taskbarEntry(page, notesId).focus();
	await page.keyboard.press('Enter');
	await expect(frame(page, NOTES)).toHaveCount(1);
	await expect(frame(page, NOTES)).toHaveAttribute('data-focused', 'true');
	await expect(taskbarEntry(page, notesId)).toHaveAttribute('aria-pressed', 'false');

	// Keyboard maximize on the prefs window. The .focus() arrival IS a real
	// focusin: it raises prefs (PT2 keyboard half), like a Tab arrival would.
	await prefs.getByRole('button', { name: 'Maximize' }).focus();
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await page.keyboard.press('Enter');
	// The toggle itself moves no focus: prefs keeps it, notes keeps none.
	await expect(prefs).toHaveAttribute('data-presentation', 'maximized');
	await expect(prefs).toHaveAttribute('data-focused', 'true');
	await expect(frame(page, NOTES)).toHaveAttribute('data-focused', 'false');
	await expect(prefs.locator('[data-resize-direction]')).toHaveCount(0);

	// Same focused control restores (label flips); geometry untouched.
	await page.keyboard.press('Enter');
	await expect(prefs).toHaveAttribute('data-presentation', 'normal');
	await expect(prefs.locator('[data-resize-direction]')).toHaveCount(8);
});

test('maximize draws the desktop-filling overlay; drag cannot commit while not normal', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	const notes = frame(page, NOTES);
	const desktop = page.getByTestId('desktop');

	const committed = await inlineGeometry(page, NOTES);
	await notes.getByRole('button', { name: 'Maximize' }).click();
	await expect(notes).toHaveAttribute('data-presentation', 'maximized');
	await expect(notes.locator('[data-resize-direction]')).toHaveCount(0);

	// The overlay fills the desktop region (frame sits inside the region's
	// 1px border, so every edge delta is ≤2px).
	await expect
		.poll(async () => {
			const region = await desktop.boundingBox();
			const overlay = await notes.boundingBox();
			if (!region || !overlay) return false;
			return (
				Math.abs(region.x - overlay.x) <= 2 &&
				Math.abs(region.y - overlay.y) <= 2 &&
				Math.abs(region.width - overlay.width) <= 2 &&
				Math.abs(region.height - overlay.height) <= 2
			);
		})
		.toBe(true);

	// The descriptor's committed geometry stays untouched (inline styles).
	await expect.poll(() => inlineGeometry(page, NOTES)).toEqual(committed);

	// Dragging the maximized title bar is NOT a restore gesture (no
	// snapping/tiling): no geometry commit, overlay stays put.
	await dragTitleBar(page, notes.locator('.mai-window-frame__titlebar'), 140, 90);
	await expect(notes).toHaveAttribute('data-presentation', 'maximized');
	await expect.poll(() => inlineGeometry(page, NOTES)).toEqual(committed);

	// Restore returns to the committed normal bounds; handles come back.
	await notes.getByRole('button', { name: 'Restore' }).click();
	await expect(notes).toHaveAttribute('data-presentation', 'normal');
	await expect(notes.locator('[data-resize-direction]')).toHaveCount(8);
	await expect.poll(() => inlineGeometry(page, NOTES)).toEqual(committed);
});

test('reload restores the effective layout: scopes, geometry, stacking, presentations (PT6)', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	const notes = frame(page, NOTES);
	const notesId = (await notes.getAttribute('data-window-id'))!;

	// Pin a distinctive geometry through the real drag path.
	await dragTitleBar(page, notes.locator('.mai-window-frame__titlebar'), 60, 50);
	const dragged = await inlineGeometry(page, NOTES);

	await openPanel(page, PREFS);
	const prefs = frame(page, PREFS);
	const prefsId = (await prefs.getAttribute('data-window-id'))!;

	// notes is NOT focused; minimizing it is view-only and prefs stays up.
	await notes.getByRole('button', { name: 'Minimize' }).click();
	await expect(frame(page, NOTES)).toHaveCount(0);
	await expect(prefs).toHaveAttribute('data-focused', 'true');

	// Maximize the remaining focused window, then persist.
	await prefs.getByRole('button', { name: 'Maximize' }).click();
	await expect(prefs).toHaveAttribute('data-presentation', 'maximized');

	// The persisted document carries the EFFECTIVE presentations.
	await expect
		.poll(async () => {
			const raw = await page.evaluate((key) => localStorage.getItem(key), LAYOUT_KEY);
			const doc = raw === null ? null : JSON.parse(raw);
			return doc === null
				? null
				: doc.windows
						.map((w: { panel_kind: string; presentation: string }) =>
							`${w.panel_kind}:${w.presentation}`
						)
						.sort()
						.join(',');
		})
		.toBe('fixture-notes:minimized,fixture-prefs:maximized');

	await page.reload();

	// Same panel kinds, fresh view identities (window_id is session-local
	// view identity, §3.1); prefs reopens maximized and focused (topmost
	// visible of the restored stacking order).
	const prefs2 = frame(page, PREFS);
	await expect(prefs2).toHaveAttribute('data-presentation', 'maximized');
	await expect(prefs2).toHaveAttribute('data-focused', 'true');
	expect(await prefs2.getAttribute('data-window-id')).not.toBe(prefsId);

	// The stored minimized window stays hidden behind its pressed entry…
	const entry2 = page.locator('.mai-window-switcher__entry', { hasText: 'Notes' });
	await expect(entry2).toHaveAttribute('aria-pressed', 'true');
	// …and the restore notice names what came back.
	await expect(page.getByTestId('desktop-status')).toHaveText('Restored 2 windows.');

	// Activating it: fresh id, pinned scope intact, committed geometry back.
	await entry2.click();
	const notes2 = frame(page, NOTES);
	await expect(notes2).toHaveAttribute('data-focused', 'true');
	expect(await notes2.getAttribute('data-window-id')).not.toBe(notesId);
	await expect(notes2.getByRole('heading')).toHaveText('Notes — Server Alpha');
	await expect(notes2.getByTestId('fixture-panel')).toContainText(
		`Pinned target: server ${SERVER_ALPHA_ID}`
	);
	await expect.poll(() => inlineGeometry(page, NOTES)).toEqual(dragged);
});

test('hostile stored layout is discarded with a notice and the desktop stays usable (NT4)', async ({
	page
}) => {
	await page.addInitScript(
		({ key }) => {
			localStorage.setItem(
				key,
				JSON.stringify({ schema_version: 99, revision: 3, windows: [] })
			);
		},
		{ key: LAYOUT_KEY }
	);

	await page.goto('/');
	await expect(frame(page, NOTES)).toHaveCount(0);
	await expect(page.getByTestId('desktop-status')).toHaveText(
		'Stored workspace layout was discarded (unknown_schema_version).'
	);
	// A whole-document discard clears the stored copy (no poison remains).
	await expect
		.poll(() => page.evaluate((key) => localStorage.getItem(key), LAYOUT_KEY))
		.toBeNull();

	// The desktop remains fully usable afterwards.
	await openPanel(page, NOTES);
	await expect(frame(page, NOTES)).toHaveCount(1);
	await expect(frame(page, NOTES).getByTestId('fixture-interactions')).toHaveText(
		'Fixture interactions: 0'
	);
});

test('viewport reflow keeps title bars recoverable and floors sizes; growth is a no-op (NT3)', async ({
	page
}) => {
	await page.goto('/');
	await openPanel(page, NOTES);
	const notes = frame(page, NOTES);

	// Drag the window toward the bottom-right of the 1280×720 viewport.
	await dragTitleBar(page, notes.locator('.mai-window-frame__titlebar'), 620, 380);

	// Shrink hard: reflow re-clamps so ≥64×32 title bar stays visible and
	// the frame never goes below the minimum-size floor (320×200 default).
	await page.setViewportSize({ width: 480, height: 360 });
	await expect
		.poll(async () => {
			const box = await notes.boundingBox();
			const vp = page.viewportSize();
			if (!box || !vp) return false;
			return (
				box.x <= vp.width - 64 &&
				box.x + box.width >= 64 &&
				box.y <= vp.height - 32 &&
				box.y + box.height >= 32
			);
		})
		.toBe(true);
	expect((await notes.boundingBox())!.width).toBeGreaterThanOrEqual(320);
	expect((await notes.boundingBox())!.height).toBeGreaterThanOrEqual(200);

	// Growing back commits nothing (no-op reflow: no spurious layout writes).
	const clamped = await inlineGeometry(page, NOTES);
	await page.setViewportSize({ width: 1280, height: 720 });
	await page.waitForTimeout(50);
	await expect.poll(() => inlineGeometry(page, NOTES)).toEqual(clamped);
});

test('broken panel: contained error view, retry stays contained, siblings unaffected', async ({
	page
}) => {
	const pageErrors: string[] = [];
	page.on('pageerror', (error) => pageErrors.push(String(error)));

	await page.goto('/');
	await openPanel(page, NOTES);

	// Bump the sibling counter BEFORE opening the broken window: the new
	// frame opens above it and would otherwise intercept the click.
	await frame(page, NOTES).getByTestId('fixture-interactions').click();
	await expect(frame(page, NOTES).getByTestId('fixture-interactions')).toHaveText(
		'Fixture interactions: 1'
	);

	await openPanel(page, BROKEN);
	const broken = frame(page, BROKEN);

	// The safe error view with Retry; raw error detail never reaches the DOM.
	await expect(broken.getByTestId('panel-load-failed')).toBeVisible();
	await expect(broken).not.toContainText('always fails');

	// Retry re-invokes the deterministic loader and stays contained.
	await broken.getByTestId('panel-load-retry').click();
	await expect(broken.getByTestId('panel-load-failed')).toBeVisible();

	// The sibling window kept its mounted content the whole time.
	await expect(frame(page, NOTES).getByTestId('fixture-interactions')).toHaveText(
		'Fixture interactions: 1'
	);

	// PT5 on the broken window: the frame close control still works.
	await broken.getByRole('button', { name: 'Close' }).click();
	await expect(frame(page, BROKEN)).toHaveCount(0);
	await expect(page.locator('.mai-window-switcher__entry', { hasText: 'Broken' })).toHaveCount(0);

	// Containment: zero unhandled page errors through the whole journey.
	expect(pageErrors).toEqual([]);
});
