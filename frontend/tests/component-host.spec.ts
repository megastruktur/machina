import { expect, test } from '@playwright/test';

// One real mount proof (test policy "Early UI bootstrap"): the labeled fixture
// host is server-rendered inert with data-mounted="false"; only a real client
// mount in the browser flips it to "true". toHaveAttribute retries until the
// flip happens, so a static/unmounted page fails instead of passing.
test('initial shell renders and mounts the labeled component host', async ({ page }) => {
	await page.goto('/');

	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Machina');

	const host = page.getByTestId('component-host');
	await expect(host).toHaveAttribute('aria-label', 'Machina workspace');
	await expect(host.getByTestId('component-host-label')).toHaveText('Machina workspace');
	await expect(host).toHaveAttribute('data-mounted', 'true');
});
