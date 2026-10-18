import { expect, test, type Page } from '@playwright/test';

/**
 * 01-ui-tokens — browser proofs for the shared token layer
 * (frontend/src/lib/ui/tokens.css, loaded once by src/routes/+layout.svelte).
 *
 * Acceptance coverage (brief 1–2, P01 scenario "render actual SvelteKit
 * baseline with focus and status examples at normal/zoomed viewport; inspect
 * contrast/focus; body styles do not break scroll or form input"):
 *  - single load: token `:root` layer declared exactly once, values resolve
 *  - frozen size constants: ADR NT3 title-bar minimum, WCAG target minimum
 *  - keyboard focus: visible ring from the :focus-visible baseline, ≥3:1
 *  - status/error states: WCAG AA text contrast (≥4.5:1) computed from the
 *    real rendered colors, not from the palette source
 *  - reduced-motion baseline: collapses transitions/animations under
 *    `prefers-reduced-motion: reduce` (negative direction asserted in normal
 *    mode)
 *  - reset safety: page stays scrollable; form input focuses, accepts text,
 *    inherits the base voice
 *  - zoomed/tight viewport: focus ring, scroll and input still work under a
 *    2× device-scale / 640×400 CSS-px emulation
 *
 * The examples are probe elements appended to the real production build of
 * the app (playwright.config.ts webServer = vite build + preview): same SSR
 * shell, same stylesheets, no invented backend.
 */

/** Append probe markup (all top-level nodes) to the live document body. */
async function append(page: Page, html: string) {
	await page.evaluate((markup) => {
		const tpl = document.createElement('template');
		tpl.innerHTML = markup.trim();
		// Appending the DocumentFragment moves every top-level node, so probe
		// markup may carry a <style> plus probe elements.
		document.body.appendChild(tpl.content);
	}, html);
}

test('tokens load exactly once through the layout and resolve on :root', async ({ page }) => {
	await page.goto('/');

	const result = await page.evaluate(() => {
		const sentinel = '--mai-color-text';
		let rootDecls = 0;
		let sheets = 0;
		for (const sheet of Array.from(document.styleSheets)) {
			let rules: CSSRuleList;
			try {
				rules = sheet.cssRules;
			} catch {
				continue; // cross-origin sheet — none expected on this fixture
			}
			sheets++;
			for (const rule of Array.from(rules)) {
				if (
					rule instanceof CSSStyleRule &&
					rule.selectorText?.split(',').some((s) => s.trim() === ':root') &&
					rule.style.getPropertyValue(sentinel) !== ''
				) {
					rootDecls++;
				}
			}
		}
		return {
			sheets,
			rootDecls,
			text: getComputedStyle(document.documentElement).getPropertyValue(sentinel).trim()
		};
	});

	expect(result.sheets, 'app stylesheets are present').toBeGreaterThan(0);
	expect(
		result.rootDecls,
		'token :root layer is declared exactly once — loaded once, no duplicated theme rules'
	).toBe(1);
	expect(result.text, 'token value resolves on the document element').toBe('#101828');
});

test('frozen size constants are exposed (NT3 title-bar minimum, target minimum)', async ({
	page
}) => {
	await page.goto('/');

	const vars = await page.evaluate(() => {
		const cs = getComputedStyle(document.documentElement);
		return {
			titlebarW: cs.getPropertyValue('--mai-titlebar-min-w').trim(),
			titlebarH: cs.getPropertyValue('--mai-titlebar-min-h').trim(),
			targetMin: cs.getPropertyValue('--mai-target-min').trim()
		};
	});

	// windowed-workspace ADR §10.2 NT3: value is contractual (64×32 CSS px).
	expect(vars.titlebarW).toBe('64px');
	expect(vars.titlebarH).toBe('32px');
	expect(vars.targetMin).toBe('24px');
});

test('keyboard focus shows a visible, high-contrast ring from the :focus-visible baseline', async ({
	page
}) => {
	await page.goto('/');
	await append(
		page,
		'<button data-testid="probe-focus" style="padding:8px 12px">Focus probe</button>'
	);

	// Fresh page: the probe button is the only tabbable element, so Tab must
	// land on it — a keyboard-triggered :focus-visible match.
	await page.keyboard.press('Tab');

	const ring = await page.evaluate(() => {
		const el = document.querySelector<HTMLElement>('[data-testid="probe-focus"]');
		if (!el || document.activeElement !== el) return null;
		const cs = getComputedStyle(el);
		return {
			style: cs.outlineStyle,
			width: cs.outlineWidth,
			color: cs.outlineColor,
			offset: cs.outlineOffset,
			bg: getComputedStyle(document.body).backgroundColor
		};
	});

	expect(ring, 'Tab focused the probe').not.toBeNull();
	expect(ring!.style, 'focus ring is drawn (outline style solid)').toBe('solid');
	expect(parseFloat(ring!.width), 'focus ring is at least the token width').toBeGreaterThanOrEqual(
		2
	);

	// ≥3:1 non-text contrast of the ring against the page background,
	// computed from the rendered colors.
	const contrast = await page.evaluate(() => {
		const lum = (rgb: number[]) => {
			const [r, g, b] = rgb.map((c) => {
				const v = c / 255;
				return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
			});
			return 0.2126 * r + 0.7152 * g + 0.0722 * b;
		};
		const parse = (s: string) =>
			(s.match(/rgba?\(([^)]+)\)/) ?? [])[1]?.split(',').slice(0, 3).map(Number) ?? null;
		const el = document.querySelector<HTMLElement>('[data-testid="probe-focus"]');
		const cs = getComputedStyle(el!);
		const fg = parse(cs.outlineColor)!;
		const bg = parse(getComputedStyle(document.body).backgroundColor)!;
		const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
		return (a + 0.05) / (b + 0.05);
	});
	expect(contrast, 'focus ring ≥3:1 against page background').toBeGreaterThanOrEqual(3);
});

test('status color pairs meet WCAG AA text contrast in the rendered page', async ({ page }) => {
	await page.goto('/');

	// Probe spans consume the same custom properties panels will use; the
	// contrast is computed from the actual computed colors.
	const pairs = await page.evaluate(() => {
		const lum = (rgb: number[]) => {
			const [r, g, b] = rgb.map((c) => {
				const v = c / 255;
				return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
			});
			return 0.2126 * r + 0.7152 * g + 0.0722 * b;
		};
		const parse = (s: string) =>
			(s.match(/rgba?\(([^)]+)\)/) ?? [])[1]?.split(',').slice(0, 3).map(Number) ?? null;
		const ratio = (fg: string, bg: string) => {
			const [a, b] = [lum(parse(fg)!), lum(parse(bg)!)].sort((x, y) => y - x);
			return (a + 0.05) / (b + 0.05);
		};
		const statuses = ['info', 'success', 'warning', 'danger', 'neutral'];
		const out: Array<{ pair: string; ratio: number }> = [];
		for (const s of statuses) {
			const tint = document.createElement('span');
			tint.style.color = `var(--mai-status-${s}-text)`;
			tint.style.backgroundColor = `var(--mai-status-${s}-bg)`;
			const solid = document.createElement('span');
			solid.style.color = `var(--mai-status-solid-contrast)`;
			solid.style.backgroundColor = `var(--mai-status-${s}-solid)`;
			document.body.append(tint, solid);
			for (const [el, pair] of [
				[tint, `${s} text on tint`],
				[solid, `contrast on ${s} solid`]
			] as const) {
				const cs = getComputedStyle(el);
				out.push({ pair, ratio: ratio(cs.color, cs.backgroundColor) });
			}
		}
		return out;
	});

	for (const { pair, ratio } of pairs) {
		expect(ratio, `${pair} meets WCAG AA for normal text`).toBeGreaterThanOrEqual(4.5);
	}
});

test('motion tokens apply in normal mode (negative direction for reduced-motion)', async ({
	page
}) => {
	await page.goto('/');
	await append(
		page,
		`<style>@keyframes tok-spin { from { opacity: 1 } to { opacity: 0 } }</style>
		 <div data-testid="probe-motion" style="animation: tok-spin 2s linear infinite; transition: opacity var(--mai-motion-base) linear"></div>`
	);

	const motion = await page.evaluate(() => {
		const cs = getComputedStyle(document.querySelector<HTMLElement>('[data-testid="probe-motion"]')!);
		return {
			transition: parseFloat(cs.transitionDuration),
			animation: parseFloat(cs.animationDuration),
			iteration: cs.animationIterationCount
		};
	});

	// Normal mode keeps motion: proves the reduced-motion block below is the
	// thing collapsing durations, not the absence of any motion at all.
	expect(motion.transition).toBeCloseTo(0.18, 5);
	expect(motion.animation).toBeCloseTo(2, 5);
	expect(motion.iteration).toBe('infinite');
});

test('reduced-motion baseline collapses transitions and animations', async ({ page }) => {
	await page.emulateMedia({ reducedMotion: 'reduce' });
	await page.goto('/');
	await append(
		page,
		`<style>@keyframes tok-spin { from { opacity: 1 } to { opacity: 0 } }</style>
		 <div data-testid="probe-motion" style="animation: tok-spin 2s linear infinite; transition: opacity var(--mai-motion-base) linear"></div>`
	);

	const motion = await page.evaluate(() => {
		const cs = getComputedStyle(document.querySelector<HTMLElement>('[data-testid="probe-motion"]')!);
		return {
			transition: parseFloat(cs.transitionDuration),
			animation: parseFloat(cs.animationDuration),
			iteration: cs.animationIterationCount
		};
	});

	expect(motion.transition, 'transition duration collapsed').toBeLessThan(0.001);
	expect(motion.animation, 'animation duration collapsed').toBeLessThan(0.001);
	expect(motion.iteration, 'looping animation no longer loops (ADR §6)').toBe('1');
});

test('body reset keeps the page scrollable', async ({ page }) => {
	await page.goto('/');
	await append(page, '<div data-testid="tall" style="height:250vh"></div>');

	const scroll = await page.evaluate(() => {
		const before = window.scrollY;
		window.scrollTo(0, 400);
		return {
			moved: window.scrollY > before,
			scrollable:
				document.documentElement.scrollHeight > document.documentElement.clientHeight,
			bodyOverflow: getComputedStyle(document.body).overflow,
			htmlOverflow: getComputedStyle(document.documentElement).overflow
		};
	});

	expect(scroll.scrollable, 'document is scrollable').toBe(true);
	expect(scroll.moved, 'programmatic scroll moves the page').toBe(true);
	expect(scroll.bodyOverflow === 'hidden' || scroll.htmlOverflow === 'hidden').toBe(false);
});

test('form input inherits the base voice and accepts text', async ({ page }) => {
	await page.goto('/');
	await append(page, '<input data-testid="probe-input" placeholder="type here" />');

	const input = page.getByTestId('probe-input');
	await input.click();
	await page.keyboard.type('mai tokens');
	await expect(input).toHaveValue('mai tokens');

	const voice = await page.evaluate(() => {
		const cs = getComputedStyle(document.querySelector<HTMLElement>('[data-testid="probe-input"]')!);
		const body = getComputedStyle(document.body);
		return { inputSize: cs.fontSize, bodySize: body.fontSize, inputColor: cs.color, bodyColor: body.color };
	});
	expect(voice.inputSize, 'input inherits base type size').toBe(voice.bodySize);
	expect(voice.inputColor, 'input inherits base text color').toBe(voice.bodyColor);
});

test('zoomed/tight viewport: focus ring, scroll and input remain usable', async ({ page }) => {
	// Emulates a ~200% browser-zoom rendering path on a small desktop layout
	// viewport (640×400 CSS px, device scale 2). Real Ctrl+zoom inspection at
	// normal desktop sizes belongs to the assembled 01-verify scenario.
	const cdp = await page.context().newCDPSession(page);
	await cdp.send('Emulation.setDeviceMetricsOverride', {
		width: 640,
		height: 400,
		deviceScaleFactor: 2,
		mobile: false
	});
	try {
		await page.goto('/');
		await append(
			page,
			`<button data-testid="probe-focus" style="padding:8px 12px">Focus probe</button>
			 <input data-testid="probe-input" placeholder="type here" />
			 <div data-testid="tall" style="height:250vh"></div>`
		);
		await page.keyboard.press('Tab');

		const probe = await page.evaluate(() => {
			const btn = document.querySelector<HTMLElement>('[data-testid="probe-focus"]');
			const input = document.querySelector<HTMLElement>('[data-testid="probe-input"]');
			window.scrollTo(0, 300);
			return {
				focused: document.activeElement === btn,
				ringWidth: parseFloat(getComputedStyle(btn!).outlineWidth),
				ringStyle: getComputedStyle(btn!).outlineStyle,
				scrolled: window.scrollY > 0,
				inputSize: getComputedStyle(input!).fontSize,
				bodySize: getComputedStyle(document.body).fontSize
			};
		});
		// Move focus off the first tab stop (the button) into the input,
		// mirroring the keyboard path: second Tab stop is the input.
		const input = page.getByTestId('probe-input');
		await page.keyboard.press('Tab');
		expect(
			await page.evaluate(() => document.activeElement?.matches('[data-testid="probe-input"]')),
			'second Tab lands in the input'
		).toBe(true);
		await page.keyboard.type('zoom');
		await expect(input).toHaveValue('zoom');

		expect(probe.focused, 'keyboard focus lands on the probe').toBe(true);
		expect(probe.ringStyle, 'focus ring still drawn when zoomed').toBe('solid');
		expect(probe.ringWidth, 'focus ring stays ≥2 CSS px when zoomed').toBeGreaterThanOrEqual(2);
		expect(probe.scrolled, 'page still scrolls when zoomed').toBe(true);
		expect(probe.inputSize, 'input still inherits base type when zoomed').toBe(probe.bodySize);
	} finally {
		await cdp.send('Emulation.clearDeviceMetricsOverride');
	}
});
