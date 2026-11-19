import {
	expect,
	test,
	type Page,
	type Route,
	type WebSocketRoute
} from '@playwright/test';

/**
 * session-title.spec.ts — ST: the generated session title surfaces.
 *
 * Driven against the ACTUAL app route (`/` rendering DesktopShell) with
 * the API + session channel mocked at the network boundary — the same
 * discipline as desktop-shell.spec.ts (no virtual fixtures): the REAL
 * shell wiring runs, including the committed-entries loop.
 *
 * Proven here:
 *  1. the active chat header (`session-header-title`) shows the HONEST
 *     id-based fallback (`session aaaaaaaa…`) while `title` is null, the
 *     visually-silent `session-header-id` is preserved, and the drawer
 *     row keeps its own `Session aaaaaaaa…` fallback;
 *  2. a stored `title` in `readAgentSession` renders verbatim in the
 *     header;
 *  3. ONE committed terminal `run_state` (succeeded) triggers exactly
 *     ONE bounded re-read of `readAgentSession` and ONE drawer page
 *     reload — the generated title lands in the header AND the drawer
 *     row without any manual refresh; a snapshot replay (same seq)
 *     never re-triggers.
 *
 * No polling loops are armed anywhere in this spec: the only
 * asynchronous driver is the mocked channel socket pushing the frames
 * the real backend would push.
 */

const SESSION = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const SERVER = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const FALLBACK = 'session aaaaaaaa…';
const GENERATED_TITLE = 'Deploy the stack';

type SnapshotState = {
	title: string | null;
	detailReads: number;
	listReads: number;
	terminalPushed: boolean;
};

function snapshotFrame(state: SnapshotState): Record<string, unknown> {
	const userEntry = {
		entry_id: '11111111-1111-4111-8111-111111111111',
		session_id: SESSION,
		run_id: null,
		seq: 1,
		kind: 'user_message',
		payload: {
			kind: 'user_message',
			text: 'How do I deploy the stack?',
			client_request_id: '22222222-2222-4222-8222-222222222223',
			source: 'interactive'
		},
		durable_source_id: 'turn:22222222-2222-4222-8222-222222222223',
		created_at: '2026-01-01T00:00:00Z'
	};
	const entries: Array<Record<string, unknown>> = [userEntry];
	if (state.terminalPushed) {
		entries.push({
			entry_id: '11111111-1111-4111-8111-111111111112',
			session_id: SESSION,
			run_id: '33333333-3333-4333-8333-333333333333',
			seq: 2,
			kind: 'run_state',
			payload: {
				kind: 'run_state',
				state: 'succeeded',
				reason_code: null,
				detail: null
			},
			durable_source_id: 'run_state:33333333-3333-4333-8333-333333333333',
			created_at: '2026-01-01T00:00:01Z'
		});
	}
	return {
		type: 'snapshot',
		session_id: SESSION,
		entries,
		latest_seq: entries.length,
		has_more: false,
		has_older: false,
		byte_size: 512,
		truncated_by_bytes: false
	};
}

function sessionRow(state: SnapshotState): Record<string, unknown> {
	return {
		session_id: SESSION,
		server_id: SERVER,
		source: 'interactive',
		status: 'idle',
		title: state.title,
		service_id: null,
		service_name: null,
		active_run_id: null,
		tag_preview: null,
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z'
	};
}

/**
 * Open the production route with:
 *  - the readable `mai_csrf` cookie + the restore pointer (the shell
 *    attaches the fixture session on connect, exactly like a reload);
 *  - routed REST reads (`/api/v1/servers`, the session list and detail
 *    pages — the counters expose every read);
 *  - a mocked session-channel WebSocket speaking the hello/snapshot
 *    frames the real backend speaks; the fixture holds the socket so a
 *    test can push further frames.
 */
async function openShell(
	page: Page,
	options: { title: string | null }
): Promise<{
	setTitle(next: string | null): void;
	detailReads(): number;
	listReads(): number;
	pushTerminalRunState(): void;
	replaySameSnapshot(): void;
}> {
	const state: SnapshotState = {
		title: options.title,
		detailReads: 0,
		listReads: 0,
		terminalPushed: false
	};
	let channel: WebSocketRoute | null = null;

	await page.addInitScript(
		([sid, srv]) => {
			document.cookie = 'mai_csrf=e2e-csrf-value; path=/; SameSite=Lax';
			window.sessionStorage.setItem(
				'mai-active-session',
				JSON.stringify({ session_id: sid, server_id: srv })
			);
		},
		[SESSION, SERVER] as const
	);

	await page.route(/\/api\/v1\/servers(\?.*)?$/, (route: Route) =>
		route.fulfill({
			contentType: 'application/json',
			body: JSON.stringify({
				servers: [
					{
						schema_version: 1,
						server_id: SERVER,
						name: 'Server Alpha',
						prompt_revision: 3,
						status: 'active',
						updated_at: '2026-01-01T00:00:00Z'
					}
				]
			})
		})
	);
	await page.route(/\/api\/v1\/agent-sessions\?/, (route: Route) => {
		state.listReads += 1;
		route.fulfill({
			contentType: 'application/json',
			body: JSON.stringify({
				items: [sessionRow(state)],
				limit: 10,
				offset: 0,
				total: 1,
				has_more: false
			})
		});
	});
	await page.route(
		new RegExp(`/api/v1/agent-sessions/${SESSION}\\?`),
		(route: Route) => {
			state.detailReads += 1;
			route.fulfill({
				contentType: 'application/json',
				body: JSON.stringify({
					...sessionRow(state),
					latest_seq: state.terminalPushed ? 2 : 1,
					total_entries: state.terminalPushed ? 2 : 1,
					compaction_count: 0,
					entries: []
				})
			});
		}
	);

	await page.routeWebSocket(
		new RegExp(`/api/v1/agent-sessions/${SESSION}/channel`),
		(ws) => {
			channel = ws;
			ws.onMessage((message) => {
				const frame: { type?: unknown } = JSON.parse(String(message));
				if (frame.type === 'hello') {
					ws.send(
						JSON.stringify({
							type: 'hello_ok',
							protocol_version: 1,
							session_id: SESSION,
							server_id: SERVER,
							stream_id: 'e2e-title-stream',
							latest_seq: state.terminalPushed ? 2 : 1
						})
					);
					ws.send(JSON.stringify(snapshotFrame(state)));
				} else if (frame.type === 'ping') {
					ws.send(JSON.stringify({ type: 'pong' }));
				}
				// subscribe/ack: the snapshot already carried the full tail.
			});
		}
	);

	await page.goto('/');

	function send(frame: Record<string, unknown>): void {
		if (channel === null) throw new Error('channel socket not opened yet');
		channel.send(JSON.stringify(frame));
	}

	return {
		setTitle(next) {
			state.title = next;
		},
		detailReads: () => state.detailReads,
		listReads: () => state.listReads,
		pushTerminalRunState() {
			// The backend order: the title lands durably BEFORE the terminal
			// run_state commits — mirror it exactly.
			state.title = GENERATED_TITLE;
			state.terminalPushed = true;
			send(snapshotFrame(state));
		},
		replaySameSnapshot() {
			// The IDENTICAL snapshot goes out again; state is untouched, so
			// the seq guard must not re-trigger anything.
			send(snapshotFrame(state));
		}
	};
}

test.describe('session title surfaces (ST)', () => {
	test('the header shows the honest fallback while the title is null', async ({
		page
	}) => {
		const fixture = await openShell(page, { title: null });
		const headerTitle = page.getByTestId('session-header-title');
		await expect(headerTitle).toBeVisible(); // the VISIBLE headline
		await expect(headerTitle).toHaveText(FALLBACK);
		// The refresh-continuity id stays in the DOM, unchanged in text.
		await expect(page.getByTestId('session-header-id')).toHaveText(FALLBACK);
		// The drawer row keeps its OWN fallback label (no title yet).
		await expect(page.getByTestId('drawer-history-item')).toHaveText(
			/Session aaaaaaaa…/
		);
		// Exactly the initial bounded reads: one detail, one list page.
		await expect.poll(() => fixture.detailReads()).toBe(1);
		await expect.poll(() => fixture.listReads()).toBe(1);
	});

	test('a stored title renders verbatim in the header', async ({ page }) => {
		await openShell(page, { title: GENERATED_TITLE });
		const headerTitle = page.getByTestId('session-header-title');
		await expect(headerTitle).toBeVisible();
		await expect(headerTitle).toHaveText(GENERATED_TITLE);
		// The drawer row label carries the title (plus chrome: chevron + status).
		await expect(page.getByTestId('drawer-history-item')).toContainText(
			GENERATED_TITLE
		);
	});

	test('one terminal run_state refreshes header and drawer exactly once', async ({
		page
	}) => {
		const fixture = await openShell(page, { title: null });
		const headerTitle = page.getByTestId('session-header-title');
		await expect(headerTitle).toHaveText(FALLBACK);
		expect(await fixture.detailReads()).toBe(1);

		// The run settles: ONE terminal run_state commits. The mock flips
		// the stored title exactly like the backend's title step would
		// have written it before the state entry landed, then pushes the
		// snapshot carrying the new entry.
		fixture.pushTerminalRunState();

		// EXACTLY one re-read of the detail and one drawer page reload.
		await expect.poll(() => fixture.detailReads()).toBe(2);
		await expect.poll(() => fixture.listReads()).toBe(2);
		await expect(headerTitle).toHaveText(GENERATED_TITLE);
		await expect(page.getByTestId('drawer-history-item')).toContainText(
			GENERATED_TITLE
		);

		// A snapshot REPLAY (same seqs) never re-triggers: still 2 reads.
		fixture.replaySameSnapshot();
		await page.waitForTimeout(400);
		expect(await fixture.detailReads()).toBe(2);
		expect(await fixture.listReads()).toBe(2);
		await expect(headerTitle).toHaveText(GENERATED_TITLE);
	});
});
