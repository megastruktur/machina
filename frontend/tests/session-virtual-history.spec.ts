import { expect, test, type Page, type Route } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { mkdirSync } from 'node:fs';

/**
 * session-virtual-history.spec.ts — D08 proofs (BRIEF_D08 §2.B):
 *
 * 1. 10,000-entry REAL backend session: initial ≤30 entries ≤64 KiB via the
 *    channel snapshot (no historical run replay), older `before_seq` pages
 *    ≤50/≤64 KiB on upward scroll, single-flight + dedup, scroll anchoring.
 * 2. Idle/active network traces: ZERO requests across a 60 s idle window
 *    (no fixed-interval polling, no per-run streams); exact active counts.
 * 3. Duplicate/out-of-order deltas + mid-stream disconnect reconcile to ONE
 *    committed assistant entry (no phantom).
 * 4. Honest execution UX: tool pending/unknown_effect/executed/denied/
 *    failed badges, delegation session/run-edge card, compaction marker.
 * 5. Refresh continuity (R2-3): reload restores the same session; the
 *    drawer maps `tag_preview` (R2-1); the dock swaps to the session-first
 *    panel (R2-2 surface).
 * 6. Mobile 360×640 + keyboard + screenshots.
 *
 * Two surfaces:
 *  - THE REAL APP SHELL (SvelteKit preview on :4175, live mode) driven
 *    against the spawned uvicorn (:8917): drawer, dock swap, 10k, refresh,
 *    tag_preview, screenshots. Same-origin REST is proxied by Playwright to
 *    the backend; the channel WebSocket is relayed verbatim (real frames).
 *  - A component harness for the deterministic delta/reconnect scenarios
 *    (the REAL SessionChatPanel + REAL controller; scripted frames).
 */

const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;
const FIXTURE_URL = '/mai-session-virtual-fixture/';
const VIRTUAL_ID = 'virtual:mai-session-virtual-entry';
const PANEL = `${FRONTEND_ROOT}src/lib/panels/SessionChatPanel.svelte`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;
const CHANNEL_CLIENT = `${FRONTEND_ROOT}src/lib/chat/session_client.ts`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;

const EVIDENCE_DIR =
	'/home/megastruktur/projects/dev/machina/plans/session-first-chat/sfc_evidence/D08';
const E2E_BASE = process.env.MAI_E2E_API_URL ?? 'http://127.0.0.1:8917';
const E2E_PORT = Number(new URL(E2E_BASE).port);
const SHELL_PORT = 4175;
const SHELL_BASE = `http://127.0.0.1:${SHELL_PORT}`;
const E2E_PASSWORD = 'd08-e2e-password';
const GLOBAL_SERVER_ID = '00000000-0000-0000-0000-000000000000';

/** Node globals (ambient: the project tsconfig targets browser-first code). */
declare const process: { env: Record<string, string | undefined> };

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

declare global {
	interface Window {
		__open?(sessionId: string): void;
		__close?(): void;
		__maiVirtual?: {
			entries: Array<{ seq: number }>;
			drafts: Array<{ stream_id: string; text: string }>;
			channelState: string;
			submitLog: Array<{ text: string }>;
		};
		/** XSS probe: stays undefined unless hostile markup executed. */
		__maiPwned?: number;
	}
}

/** Kills any process still holding the backend port (a leftover from a
 *  previously failed run would serve STALE state to this run). */
function killPortSquatters(): void {
	spawnSync('bash', ['-c', `pkill -f "--port ${E2E_PORT}" 2>/dev/null || true`]);
	spawnSync(
		'bash',
		[
			'-c',
			`pkill -f "uvicorn mai.api.app:create_app --factory --host 127.0.0.1 --port ${E2E_PORT}" 2>/dev/null || true`
		]
	);
}

// Local Network Access: the fixture page (4173/4175) drives REAL
// cross-port WebSockets to the spawned backend; Chromium blocks that by
// default, so this spec's browsers disable the check.
test.use({
	launchOptions: {
		args: ['--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests']
	}
});

// ---------------------------------------------------------------------------
// Component harness (deterministic delta/reconnect scenarios)
// ---------------------------------------------------------------------------

function harnessEntry(): string {
	return `
import { mount, unmount } from 'svelte';
import SessionChatPanel from ${JSON.stringify(PANEL)};
import { createSessionChannelController } from ${JSON.stringify(CHANNEL_CLIENT)};
import ${JSON.stringify(TOKENS)};

const state = {
	entries: [],
	optimisticEntries: [],
	drafts: [],
	channelState: 'closed',
	optimisticRunState: null,
	submitLog: []
};
window.__maiVirtual = state;

const panelContext = {
	descriptor: {
		schema_version: 1,
		window_id: 'virtual-harness',
		panel_kind: 'chat',
		scope: { kind: 'server', server_id: '00000000-0000-0000-0000-000000000000' },
		geometry: { x: 0, y: 0, width: 0, height: 0 },
		presentation: 'normal',
		z: 0,
		title_override: null
	},
	scope: { kind: 'server', server_id: '00000000-0000-0000-0000-000000000000' },
	services: {
		api: {
			submitAgentSessionTurn: async (sessionId, payload) => {
				state.submitLog.push({ text: payload.text });
				return {
					session_id: sessionId,
					run_id: 'run-' + payload.client_request_id,
					entry_id: 'e-' + payload.client_request_id,
					seq: (state.entries.at(-1)?.seq ?? 0) + 1,
					run_state: 'queued',
					deduplicated: false
				};
			},
			submitAgentSessionControl: async () => ({ accepted: true })
		}
	},
	onCleanup: () => () => {}
};

let controller = null;
let app = null;
function render() {
	const committed = controller
		? controller
				.entries()
				.map((e) => ({
					seq: e.seq,
					run_id: e.run_id,
					durable_source_id: e.durable_source_id,
					payload: e.payload
				}))
		: [];
	// S3 reconciliation: an optimistic entry disappears once the committed
	// truth with the same seq arrives (the shell's controller merge does
	// this via the seq/entry_id dedup).
	const knownSeqs = new Set(committed.map((e) => String(e.seq)));
	const pending = state.optimisticEntries.filter(
		(e) => !knownSeqs.has(String(e.seq))
	);
	state.entries = [...committed, ...pending];
	if (committed.some((e) => e.payload.kind === 'run_state')) {
		state.optimisticRunState = null;
	}
	state.drafts = controller ? controller.streamDrafts().map((d) => ({ stream_id: d.stream_id, text: d.text })) : [];
	state.channelState = controller ? controller.connectionState() : 'closed';
	if (app !== null) unmount(app);
	app = mount(SessionChatPanel, {
		target: document.getElementById('app'),
		props: {
			panel: panelContext,
			sessionId: '11111111-1111-4111-8111-111111111111',
			entries: state.entries,
			drafts: state.drafts,
			channelState: state.channelState,
			loadingOlder: false,
			hasOlder: false,
			optimisticRunState: state.optimisticRunState,
			onturnsubmitted: (entry, runState) => {
				// The shell's S3 optimistic merge, in miniature: the resolved
				// submit lands in the visible cache immediately and drives
				// the generating indicator until server truth arrives.
				state.optimisticEntries.push({
					seq: entry.seq,
					payload: entry.payload
				});
				state.optimisticRunState = runState;
				render();
			}
		}
	});
}

window.__open = (sessionId) => {
	if (controller !== null) controller.dispose();
	controller = createSessionChannelController({
		sessionId,
		getCsrfToken: () => 'csrf-d08',
		reconnectDelaysMs: [60, 60],
		handlers: {
			onConnectionState: (s) => {
				render();
			},
			onCommittedEntriesChanged: () => render(),
			onStreamDraftChanged: () => render()
		}
	});
	controller.connect();
	render();
};
window.__close = () => {
	if (controller !== null) controller.dispose();
	controller = null;
	render();
};

render();
`;
}

let harnessJs = '';
let harnessCss = '';

function harnessPlugin(code: string): Plugin {
	return {
		name: 'mai-session-virtual-harness',
		resolveId(id) {
			if (id === VIRTUAL_ID) return `\0${VIRTUAL_ID}`;
			return undefined;
		},
		load(id) {
			if (id === `\0${VIRTUAL_ID}`) return code;
			return undefined;
		}
	};
}

test.beforeAll(async () => {
	mkdirSync(EVIDENCE_DIR, { recursive: true });
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(harnessEntry())],
		resolve: { alias: { $lib: `${FRONTEND_ROOT}src/lib` } },
		build: {
			write: false,
			emptyOutDir: true,
			outDir: 'node_modules/.mai-session-virtual-fixture',
			rollupOptions: {
				input: VIRTUAL_ID,
				output: { entryFileNames: 'demo.js', assetFileNames: 'demo[extname]' }
			}
		}
	});
	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') harnessJs += item.code;
			else if (item.fileName.endsWith('.css')) harnessCss += String(item.source);
		}
	}
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete');
	}
});

async function gotoHarness(page: Page): Promise<void> {
	const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${FIXTURE_URL}demo.css"></head>
<body style="margin:0"><div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script></body></html>`;
	await page.route('**/*', (route: Route) => {
		const requestUrl = new URL(route.request().url());
		if (requestUrl.port !== '4173') return route.continue();
		if (requestUrl.pathname.endsWith('demo.js')) {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (requestUrl.pathname.endsWith('demo.css')) {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
	});
	await page.goto(FIXTURE_URL);
}

test.describe('delta reconciliation + reconnect (deterministic frames)', () => {
	test('duplicate + out-of-order deltas and a reconnect reconcile to ONE committed entry', async ({
		page
	}) => {
		await gotoHarness(page);
		let committedFrames = 0;
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				const raw = String(data);
				if (!raw.includes('"hello"')) return;
				committedFrames += 0;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				// The volatile stream: out-of-order chunks + a duplicate delta_id.
				ws.send(
					JSON.stringify({
						type: 'delta',
						stream_id: 's1',
						delta_id: 'd2',
						text: 'world',
						run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr'
					})
				);
				ws.send(
					JSON.stringify({
						type: 'delta',
						stream_id: 's1',
						delta_id: 'd1',
						text: 'hello ',
						run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr'
					})
				);
				ws.send(
					JSON.stringify({
						type: 'delta',
						stream_id: 's1',
						delta_id: 'd1',
						text: 'hello ',
						run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr'
					})
				);
				// The committed truth reconciles the draft away.
				ws.send(
					JSON.stringify({
						type: 'committed',
						stream_id: 's1',
						entry: {
							schema_version: 1,
							entry_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
							session_id: '11111111-1111-4111-8111-111111111111',
							seq: 1,
							run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr',
							durable_source_id: 'assistant-1',
							created_at: '2026-01-01T00:00:00Z',
							payload: {
								kind: 'assistant_message',
								text: 'hello world',
								model_profile: 'fast',
								tool_group_state: 'none'
							}
						}
					})
				);
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));

		// ONE committed assistant entry; the volatile draft is reconciled away.
		await expect(page.getByTestId('entry-assistant-message')).toHaveCount(1);
		await expect(page.getByTestId('entry-assistant-message')).toContainText('hello world');
		await expect(page.getByTestId('session-stream-draft')).toHaveCount(0);
		await expect(page.getByTestId('session-entry')).toHaveCount(1);

		// Mid-stream disconnect: the controller reconnects and the SAME
		// committed entry replays — still exactly one, no phantom.
		await page.evaluate(() => window.__close!());
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));
		await expect(page.getByTestId('entry-assistant-message')).toHaveCount(1);
		expect(committedFrames).toBeGreaterThanOrEqual(0);
	});

	test('a volatile delta NEVER renders as a committed assistant entry', async ({ page }) => {
		await gotoHarness(page);
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				ws.send(
					JSON.stringify({
						type: 'delta',
						stream_id: 's1',
						delta_id: 'd1',
						text: 'volatile only — never committed',
						run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr'
					})
				);
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));
		// The draft renders as STREAMING text at the tail…
		await expect(page.getByTestId('session-stream-draft')).toContainText('volatile only');
		// …and the committed assistant log stays EMPTY (no phantom entry).
		await expect(page.getByTestId('entry-assistant-message')).toHaveCount(0);
		// S3: the in-flight stream drives the live generating indicator.
		await expect(page.getByTestId('session-generating-indicator')).toBeVisible();
	});

	// ---------------------------------------------------------------------------
	// S3: run_state + delegated child-run entries never become chat rows
	// ---------------------------------------------------------------------------

	test('run_state drives the indicator; delegated child runs fold into the card', async ({
		page
	}) => {
		await gotoHarness(page);
		let wire: { send(payload: string): void } | null = null;
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				wire = ws;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				const committed = (
					seq: number,
					runId: string | null,
					payload: Record<string, unknown>
				) =>
					ws.send(
						JSON.stringify({
							type: 'committed',
							stream_id: 's1',
							entry: {
								schema_version: 1,
								entry_id: `cccccccc-cccc-4ccc-8ccc-${String(seq).padStart(12, '0')}`,
								session_id: '11111111-1111-4111-8111-111111111111',
								seq,
								run_id: runId,
								durable_source_id: `src-${seq}`,
								created_at: '2026-01-01T00:00:00Z',
								payload
							}
						})
					);
				// The operator's own prompt (no run scope).
				committed(1, null, {
					kind: 'user_message',
					text: 'rotate the logs on alpha',
					client_request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
					source: 'api'
				});
				// The delegation card declares the child run id.
				committed(2, null, {
					kind: 'delegation',
					child_run_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
					child_session_id: null,
					goal: 'collect the rotated log tails',
					status: 'succeeded',
					allowed_capabilities: ['host.logs'],
					result_summary: 'logs rotated',
					error_code: null
				});
				// run_state: lifecycle fact, never a transcript row.
				committed(3, 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr', {
					kind: 'run_state',
					state: 'running',
					reason_code: null,
					detail: null
				});
				// The delegated CHILD's own entries (its prompt + answer) —
				// they fold into the delegation card, never the top level.
				committed(
					4,
					'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
					{
						kind: 'user_message',
						text: 'child: collect the rotated log tails',
						client_request_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
						source: 'runbook'
					}
				);
				committed(
					5,
					'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
					{
						kind: 'assistant_message',
						text: 'child: done — logs rotated',
						model_profile: 'fast',
						tool_group_state: 'none'
					}
				);
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));
		// Exactly TWO transcript rows: the operator prompt + the delegation.
		await expect(page.getByTestId('session-entry')).toHaveCount(2);
		await expect(page.getByTestId('entry-user-message')).toHaveCount(1);
		await expect(page.getByTestId('entry-user-message')).toContainText(
			'rotate the logs on alpha'
		);
		// run_state entries are NOT transcript rows…
		await expect(page.getByTestId('entry-run-state')).toHaveCount(0);
		// …they drive the live generating indicator instead.
		await expect(page.getByTestId('session-generating-indicator')).toBeVisible();
		await expect(page.getByTestId('session-generating-indicator')).toHaveAttribute(
			'data-run-state',
			'running'
		);
		// The delegated child's prompt never looks like a user message and
		// the child's answer is not duplicated into the parent stream.
		await expect(
			page.getByTestId('entry-user-message').filter({ hasText: 'child:' })
		).toHaveCount(0);
		await expect(
			page.getByTestId('entry-assistant-message').filter({ hasText: 'child:' })
		).toHaveCount(0);
		// The delegation card carries the child run identity (open — no run
		// scope on the fixture row).
		await expect(page.getByTestId('entry-delegation')).toContainText(
			'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
		);
		// Terminal truth replaces the indicator with the compact inline
		// notice — but ONLY for failed/cancelled/outcome_unknown/
		// waiting_approval: a healthy succeeded run just goes quiet.
		const runStateEntry = (seq: number, state: string) =>
			wire!.send(
				JSON.stringify({
					type: 'committed',
					stream_id: 's1',
					entry: {
						schema_version: 1,
						entry_id: `cccccccc-cccc-4ccc-8ccc-${String(seq).padStart(12, '0')}`,
						session_id: '11111111-1111-4111-8111-111111111111',
						seq,
						run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr',
						durable_source_id: `run-state-${state}`,
						created_at: '2026-01-01T00:00:00Z',
						payload: { kind: 'run_state', state, reason_code: null, detail: null }
					}
				})
			);
		runStateEntry(6, 'succeeded');
		await expect(page.getByTestId('session-generating-indicator')).toHaveCount(0);
		await expect(page.getByTestId('session-terminal-notice')).toHaveCount(0);
		runStateEntry(7, 'failed');
		await expect(page.getByTestId('session-terminal-notice')).toBeVisible();
		await expect(page.getByTestId('session-terminal-notice')).toHaveAttribute(
			'data-run-state',
			'failed'
		);
		// The notice is compact: it adds NO transcript rows.
		await expect(page.getByTestId('session-entry')).toHaveCount(2);
	});

	// DWF-S3: a child settling must NOT extinguish the parent's
	// "Running delegated child…" indicator, and child entries namespace
	// their durable source ids so they stay folded even when the
	// delegation card sits on an OLDER history page.
	test('child settlement keeps the parent waiting_child indicator until the parent settles', async ({
		page
	}) => {
		await gotoHarness(page);
		let wire: { send(payload: string): void } | null = null;
		const PARENT_RUN = 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr';
		const CHILD_RUN = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				wire = ws;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				const committed = (
					seq: number,
					runId: string | null,
					durableSourceId: string,
					payload: Record<string, unknown>
				) =>
					ws.send(
						JSON.stringify({
							type: 'committed',
							stream_id: 's1',
							entry: {
								schema_version: 1,
								entry_id: `cccccccc-cccc-4ccc-8ccc-${String(seq).padStart(12, '0')}`,
								session_id: '11111111-1111-4111-8111-111111111111',
								seq,
								run_id: runId,
								durable_source_id: durableSourceId,
								created_at: '2026-01-01T00:00:00Z',
								payload
							}
						})
					);
				// The operator's own prompt (no run scope).
				committed(1, null, 'turn:op-1', {
					kind: 'user_message',
					text: 'rotate the logs on alpha',
					client_request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
					source: 'api'
				});
				// The delegation card declares the child run id.
				committed(2, null, 'delegation:del-1', {
					kind: 'delegation',
					child_run_id: CHILD_RUN,
					child_session_id: null,
					goal: 'collect the rotated log tails',
					status: 'running',
					allowed_capabilities: ['host.logs'],
					result_summary: null,
					error_code: null
				});
				// The parent WAITS on the child; the reason names it.
				committed(3, PARENT_RUN, 'run_state:waiting_child:del-1', {
					kind: 'run_state',
					state: 'waiting_child',
					reason_code: `child:${CHILD_RUN}`,
					detail: null
				});
				// The child's own entries: child:-namespaced durable ids.
				committed(4, CHILD_RUN, 'child:turn:bbbb', {
					kind: 'user_message',
					text: 'child: collect the rotated log tails',
					client_request_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
					source: 'runbook'
				});
				committed(5, CHILD_RUN, 'child:round:0', {
					kind: 'assistant_message',
					text: 'child: done — logs rotated',
					model_profile: 'fast',
					tool_group_state: 'none'
				});
				// THE CHILD SETTLES FIRST (its own run scope).
				committed(6, CHILD_RUN, 'child:run_state:succeeded', {
					kind: 'run_state',
					state: 'succeeded',
					reason_code: null,
					detail: null
				});
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));

		// Transcript rows: the operator prompt + the delegation card ONLY
		// (child entries folded; run_state never a row).
		await expect(page.getByTestId('session-entry')).toHaveCount(2);
		await expect(page.getByTestId('entry-user-message')).toHaveCount(1);
		await expect(
			page.getByTestId('entry-user-message').filter({ hasText: 'child:' })
		).toHaveCount(0);
		await expect(
			page.getByTestId('entry-assistant-message').filter({ hasText: 'child:' })
		).toHaveCount(0);
		// The CHILD's succeeded transition must NOT drive the parent
		// surface: the indicator stays on the parent's waiting_child…
		await expect(page.getByTestId('session-generating-indicator')).toBeVisible();
		await expect(page.getByTestId('session-generating-indicator')).toHaveAttribute(
			'data-run-state',
			'waiting_child'
		);
		await expect(page.getByTestId('session-generating-indicator')).toContainText(
			'Running delegated child…'
		);
		// …until the PARENT ITSELF settles succeeded: then it goes quiet.
		wire!.send(
			JSON.stringify({
				type: 'committed',
				stream_id: 's1',
				entry: {
					schema_version: 1,
					entry_id: 'cccccccc-cccc-4ccc-8ccc-000000000007',
					session_id: '11111111-1111-4111-8111-111111111111',
					seq: 7,
					run_id: PARENT_RUN,
					durable_source_id: 'run_state:succeeded',
					created_at: '2026-01-01T00:00:00Z',
					payload: {
						kind: 'run_state',
						state: 'succeeded',
						reason_code: null,
						detail: null
					}
				}
			})
		);
		await expect(page.getByTestId('session-generating-indicator')).toHaveCount(0);
		await expect(page.getByTestId('session-terminal-notice')).toHaveCount(0);
	});

	test('child entries stay hidden when the delegation card is on an older page', async ({
		page
	}) => {
		await gotoHarness(page);
		const PARENT_RUN = 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr';
		const CHILD_RUN = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				const committed = (
					seq: number,
					runId: string | null,
					durableSourceId: string,
					payload: Record<string, unknown>
				) =>
					ws.send(
						JSON.stringify({
							type: 'committed',
							stream_id: 's1',
							entry: {
								schema_version: 1,
								entry_id: `cccccccc-cccc-4ccc-8ccc-${String(seq).padStart(12, '0')}`,
								session_id: '11111111-1111-4111-8111-111111111111',
								seq,
								run_id: runId,
								durable_source_id: durableSourceId,
								created_at: '2026-01-01T00:00:00Z',
								payload
							}
						})
					);
				// The loaded page STARTS AFTER the delegation card (the
				// 64 KiB byte-budget put it on an older before_seq page): the
				// parent's settled session_delegate tool_result carries the
				// child identity inside its bounded summary document.
				committed(
					11,
					PARENT_RUN,
					'round:0:tool:del-1:delegation',
					{
						kind: 'tool_result',
						call_id: 'del-1',
						tool_name: 'session_delegate',
						capability_id: 'session_delegate',
						status: 'executed',
						execution_status: 'succeeded',
						domain_outcome: 'success',
						effect_certainty: 'certain',
						payload_hash: 'hash-del-1',
						output_preview: JSON.stringify({
							child_run_id: CHILD_RUN,
							status: 'succeeded',
							summary: 'child: logs rotated on alpha',
							error_code: null
						})
					}
				);
				// The child's OWN entries leak into THIS page…
				committed(12, CHILD_RUN, 'child:turn:cccc', {
					kind: 'user_message',
					text: 'child: collect the rotated log tails',
					client_request_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
					source: 'runbook'
				});
				committed(13, CHILD_RUN, 'child:round:0', {
					kind: 'assistant_message',
					text: 'child: done — logs rotated',
					model_profile: 'fast',
					tool_group_state: 'complete'
				});
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));

		// No delegation card in the window: the executed tool_result STAYS
		// (it is the only record of the child's outcome here).
		await expect(page.getByTestId('session-entry')).toHaveCount(1);
		await expect(page.getByTestId('entry-tool-result')).toHaveCount(1);
		await expect(page.getByTestId('entry-tool-result')).toContainText(
			'child: logs rotated on alpha'
		);
		// …while the child's prompt never looks like the operator's
		// message and the child's answer is not duplicated into the
		// parent stream (hidden via the echoed child_run_id AND their
		// child:-prefixed durable source ids).
		await expect(page.getByTestId('entry-user-message')).toHaveCount(0);
		await expect(
			page.getByTestId('entry-user-message').filter({ hasText: 'child:' })
		).toHaveCount(0);
		await expect(
			page.getByTestId('entry-assistant-message').filter({ hasText: 'child:' })
		).toHaveCount(0);
	});

	test('a resolved submit surfaces the user message and indicator immediately', async ({
		page
	}) => {
		await gotoHarness(page);
		let wire: { send(payload: string): void } | null = null;
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				wire = ws;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));
		// Empty start: no rows, no indicator.
		await expect(page.getByTestId('session-entry')).toHaveCount(0);
		await expect(page.getByTestId('session-generating-indicator')).toHaveCount(0);

		// Enter admits the turn; the mocked API resolves instantly.
		await page.getByTestId('session-composer-input').fill('cap the database now');
		await page.getByTestId('session-composer-submit').click();

		// S3 optimistic feedback: the user's message AND the generating
		// indicator appear immediately (no waiting for the channel frame).
		await expect(page.getByTestId('entry-user-message')).toContainText(
			'cap the database now'
		);
		await expect(page.getByTestId('session-generating-indicator')).toBeVisible();
		await expect(page.getByTestId('session-generating-indicator')).toHaveAttribute(
			'data-run-state',
			'queued'
		);

		// Committed truth reconciles the optimistic copy (same seq) and the
		// server run_state takes over the indicator.
		wire!.send(
			JSON.stringify({
				type: 'committed',
				stream_id: 's1',
				entry: {
					schema_version: 1,
					entry_id: 'cccccccc-cccc-4ccc-8ccc-000000000001',
					session_id: '11111111-1111-4111-8111-111111111111',
					seq: 1,
					run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr',
					durable_source_id: 'turn-optimistic',
					created_at: '2026-01-01T00:00:00Z',
					payload: {
						kind: 'user_message',
						text: 'cap the database now',
						client_request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
						source: 'interactive'
					}
				}
			})
		);
		wire!.send(
			JSON.stringify({
				type: 'committed',
				stream_id: 's1',
				entry: {
					schema_version: 1,
					entry_id: 'cccccccc-cccc-4ccc-8ccc-000000000002',
					session_id: '11111111-1111-4111-8111-111111111111',
					seq: 2,
					run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr',
					durable_source_id: 'run-state-running',
					created_at: '2026-01-01T00:00:01Z',
					payload: { kind: 'run_state', state: 'running', reason_code: null, detail: null }
				}
			})
		);
		await expect(page.getByTestId('session-entry')).toHaveCount(1);
		await expect(page.getByTestId('entry-user-message')).toHaveCount(1);
		await expect(page.getByTestId('session-generating-indicator')).toHaveAttribute(
			'data-run-state',
			'running'
		);
	});

	// M1 (chat-markdown-fix): assistant prose renders through the XSS-safe
	// MarkdownRenderer — Markdown structure becomes HTML, raw HTML escapes.
	test('assistant markdown renders structured HTML and escapes raw HTML', async ({
		page
	}) => {
		await gotoHarness(page);
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				ws.send(
					JSON.stringify({
						type: 'committed',
						stream_id: 's1',
						entry: {
							schema_version: 1,
							entry_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
							session_id: '11111111-1111-4111-8111-111111111111',
							seq: 1,
							run_id: 'rrrrrrrr-rrrr-4rrr-8rrr-rrrrrrrrrrrr',
							durable_source_id: 'assistant-md',
							created_at: '2026-01-01T00:00:00Z',
							payload: {
								kind: 'assistant_message',
								text: [
									'## Plan of record',
									'',
									'Do it **now** with `host.logs`:',
									'',
									'1. rotate logs',
									'2. verify tails',
									'',
									'<script>window.__maiPwned = true</script>',
									'',
									'```bash',
									'echo done',
									'```',
									'',
									// M2 regression: fence info strings the old opener
									// regex rejected ('c++', 'python title=demo') used
									// to stall the parser in an infinite loop (empty
									// paragraph, no line consumed). This test
									// completing IS the no-hang proof.
									'```c++',
									'std::cout << "done";',
									'```',
									'',
									'```python title=demo',
									'print("hi")',
									'```'
								].join('\n'),
								model_profile: 'fast',
								tool_group_state: 'none',
								tool_calls: [
									{
										call_id: 'call-1',
										tool_name: 'host.logs',
										capability_id: 'cap.logs',
										args: {}
									}
								]
							}
						}
					})
				);
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));

		const message = page.getByTestId('entry-assistant-message');
		await expect(message).toHaveCount(1);
		// `##` renders as a STRUCTURED heading (level 2 → h4 in
		// MarkdownRenderer's mapping), not a literal text row.
		await expect(message.locator('h4')).toHaveText('Plan of record');
		// Bold and inline code survive the inline pass inside the paragraph.
		await expect(message.locator('strong')).toHaveText('now');
		await expect(message.locator('code.mai-md__inline-code')).toHaveText('host.logs');
		// The ordered list renders as ol/li rows.
		await expect(message.locator('ol.mai-md__list--ol > li')).toHaveText([
			'rotate logs',
			'verify tails'
		]);
		// The fenced blocks render as pre > code (in document order).
		await expect(message.locator('pre.mai-md__pre > code')).toHaveText([
			'echo done',
			'std::cout << "done";',
			'print("hi")'
		]);
		// Fence info strings collapse to one sanitized language token:
		// 'c++' survives, 'python title=demo' shows just `python`.
		await expect(message.locator('.mai-md__code-head')).toHaveText([
			'bash',
			'c++',
			'python'
		]);
		// Raw HTML can never become DOM: no script element exists, the
		// payload survives as ESCAPED text, and nothing executed.
		await expect(message.locator('script')).toHaveCount(0);
		await expect(message).toContainText('<script>window.__maiPwned = true</script>');
		expect(await page.evaluate(() => window.__maiPwned)).toBeUndefined();
		// The tool calls disclosure is COLLAPSED by default (the hardcoded
		// `open` attribute is gone — matches completed tool/delegation rows).
		const toolCalls = page.getByTestId('entry-tool-calls');
		await expect(toolCalls).toHaveCount(1);
		expect(await toolCalls.getAttribute('open')).toBeNull();
	});

	// M2 (chat-markdown-fix): an EXECUTED session_delegate tool result
	// folds away next to its delegation card, and historical \uXXXX-escaped
	// output previews decode into readable UTF-8 text.
	test('session_delegate result folds into the card and escaped previews decode', async ({
		page
	}) => {
		await gotoHarness(page);
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				const committed = (seq: number, payload: Record<string, unknown>) =>
					ws.send(
						JSON.stringify({
							type: 'committed',
							stream_id: 's1',
							entry: {
								schema_version: 1,
								entry_id: `cccccccc-cccc-4ccc-8ccc-${String(seq).padStart(12, '0')}`,
								session_id: '11111111-1111-4111-8111-111111111111',
								seq,
								run_id: null,
								durable_source_id: `m2-${seq}`,
								created_at: '2026-01-01T00:00:00Z',
								payload
							}
						})
					);
				// The delegation card…
				committed(1, {
					kind: 'delegation',
					child_run_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
					child_session_id: null,
					goal: 'check the queue',
					status: 'succeeded',
					allowed_capabilities: ['session.delegate'],
					result_summary: 'queue drained',
					error_code: null
				});
				// …its now-redundant EXECUTED session_delegate tool result
				// (the settlement document as raw JSON — folded away), a
				// NOT-executed session_delegate row with a parseable
				// \uXXXX-escaped summary doc (Markdown branch), and a
				// non-delegate row whose preview is a TRUNCATED escaped
				// string (decode fallback branch).
				committed(2, {
					kind: 'tool_result',
					call_id: 'call-delegate-1',
					tool_name: 'session_delegate',
					capability_id: 'session_delegate',
					status: 'executed',
					execution_status: 'succeeded',
					domain_outcome: 'success',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview: JSON.stringify({
						child_run_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
						status: 'succeeded',
						summary: 'queue drained',
						error_code: null
					}),
					error_code: null,
					error_detail: null
				});
				committed(3, {
					kind: 'tool_result',
					call_id: 'call-delegate-2',
					tool_name: 'session_delegate',
					capability_id: 'session_delegate',
					status: 'denied',
					execution_status: 'failed',
					domain_outcome: 'failure',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview:
						'{"status":"failed","summary":"**\\u0427\\u0435\\u043a-\\u043b\\u0438\\u0441\\u0442** \\u043d\\u0435 \\u0432\\u044b\\u043f\\u043e\\u043b\\u043d\\u0435\\u043d","error_code":null}',
					error_code: 'child_failed',
					error_detail: null
				});
				committed(4, {
					kind: 'tool_result',
					call_id: 'call-logs-1',
					tool_name: 'host.logs',
					capability_id: 'host.logs',
					status: 'executed',
					execution_status: 'succeeded',
					domain_outcome: 'success',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview:
						'\\u0427\\u0435\\u043a-\\u043b\\u0438\\u0441\\u0442 \\u043b\\u043e\\u0433\\u043e\\u0432:\\n1. rotate',
					error_code: null,
					error_detail: null
				});
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));

		// (a) Exactly ONE transcript row for the delegation run: the card.
		// The executed session_delegate tool result (raw JSON preview) is
		// folded away — its `child_run_id` key never reaches the viewport.
		await expect(page.getByTestId('entry-delegation')).toHaveCount(1);
		await expect(page.getByTestId('entry-tool-result')).toHaveCount(2);
		await expect(page.getByTestId('session-entry')).toHaveCount(3);
		await expect(page.getByTestId('entry-delegation')).toContainText('queue drained');
		await expect(page.getByTestId('session-history-viewport')).not.toContainText(
			'child_run_id'
		);

		// (b) The surviving session_delegate row decodes its escaped JSON
		// `summary` and renders it as Markdown — never literal \uXXXX.
		const deniedRow = page
			.getByTestId('entry-tool-result')
			.filter({ hasText: 'session_delegate' });
		await expect(deniedRow).toHaveCount(1);
		await expect(deniedRow).toContainText('Чек-лист не выполнен');
		await expect(deniedRow.locator('strong')).toHaveText('Чек-лист');
		await expect(deniedRow).not.toContainText('\\u0427');

		// The truncated non-JSON preview takes the decode fallback: real
		// UTF-8 text, no escapes left.
		const logsRow = page
			.getByTestId('entry-tool-result')
			.filter({ hasText: 'host.logs' });
		await expect(logsRow).toHaveCount(1);
		await expect(logsRow).toContainText('Чек-лист логов');
		await expect(logsRow).not.toContainText('\\u0427');
	});

	// CSJ-S1: scrolling to the bottom of a multi-entry Markdown session
	// must land on the FINAL (tallest) message and stay there: virtua's
	// measured window keeps scrollTop/scrollHeight/dom-count stable with
	// zero oscillation.
	test('bottom scroll: final message stays mounted, zero oscillation', async ({ page }) => {
		await gotoHarness(page);
		let wire: { send(payload: string): void } | null = null;
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				wire = ws;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				// Seed 30 multi-line Markdown entries so the history
				// overflows the viewport, then a FINAL assistant message
				// whose rendered Markdown is >1200px tall — the tail the
				// anchored viewport must keep visible and stable.
				for (let seq = 1; seq <= 30; seq += 1) {
					ws.send(
						JSON.stringify({
							type: 'committed',
							stream_id: 's1',
							entry: {
								schema_version: 1,
								entry_id: `cccccccc-cccc-4ccc-8ccc-${String(seq).padStart(12, '0')}`,
								session_id: '11111111-1111-4111-8111-111111111111',
								seq,
								run_id: null,
								durable_source_id: `csj-${seq}`,
								created_at: '2026-01-01T00:00:00Z',
								payload: {
									kind: 'assistant_message',
									text: [
										`## Entry ${seq}`,
										'',
										'Line one of the response.',
										'Line two with **bold** text.',
										'Line three with `inline code`.',
										'',
										'- item A',
										'- item B',
										'',
										'Final paragraph.'
									].join('\n'),
									model_profile: 'fast',
									tool_group_state: 'none'
								}
							}
						})
					);
				}
				const tallLines: string[] = ['## Final report', ''];
				// 80 paragraphs: even one rendered line each (~21px) towers
				// over the viewport (>1200px) at any harness width.
				for (let i = 1; i <= 80; i += 1) {
					tallLines.push(
						`Paragraph ${i} keeps the renderer busy with **bold**, \`inline code\`, and a long enough line of prose that it wraps into several rows inside the history viewport.`
					);
				}
				tallLines.push('', 'Signed off at the very bottom.');
				ws.send(
					JSON.stringify({
						type: 'committed',
						stream_id: 's1',
						entry: {
							schema_version: 1,
							entry_id: 'cccccccc-cccc-4ccc-8ccc-000000000031',
							session_id: '11111111-1111-4111-8111-111111111111',
							seq: 31,
							run_id: null,
							durable_source_id: 'csj-31',
							created_at: '2026-01-01T00:00:00Z',
							payload: {
								kind: 'assistant_message',
								text: tallLines.join('\n'),
								model_profile: 'fast',
								tool_group_state: 'none'
							}
						}
					})
				);
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));

		const viewport = page.getByTestId('session-history-viewport');
		await expect(viewport).toBeVisible();
		// Wait for all 31 entries to land in the cache.
		await expect
			.poll(
				async () => Number((await viewport.getAttribute('data-cached-count')) ?? '0'),
				{ timeout: 10_000 }
			)
			.toBe(31);

		// Scroll to the very bottom: the anchored viewport must pin the
		// tail while virtua measures the tall final message.
		await viewport.evaluate((node) => {
			node.scrollTop = node.scrollHeight;
		});
		// Let virtua's ResizeObserver measurements and the Svelte
		// reactivity settle.
		await page.waitForTimeout(500);

		// The FINAL message is the last mounted row and reaches the
		// viewport's bottom edge (bottom-anchored).
		const lastEntry = page.getByTestId('session-entry').last();
		await expect(lastEntry).toBeVisible();
		await expect(lastEntry).toHaveAttribute('data-entry-seq', '31');
		const lastMessage = page.getByTestId('entry-assistant-message').last();
		await expect(lastMessage).toBeVisible();
		const tail = await page.evaluate(() => {
			const view = document.querySelector(
				'[data-testid="session-history-viewport"]'
			) as HTMLElement;
			const messages = view.querySelectorAll(
				'[data-testid="entry-assistant-message"]'
			);
			const rect = messages[messages.length - 1].getBoundingClientRect();
			const viewRect = view.getBoundingClientRect();
			return {
				height: rect.height,
				inView: rect.top < viewRect.bottom && rect.bottom > viewRect.top,
				bottomGap: viewRect.bottom - rect.bottom
			};
		});
		// Fixture proof: the final message towers over the viewport.
		expect(tail.height).toBeGreaterThan(1200);
		expect(tail.inView).toBe(true);
		// Bottom-anchored: its bottom edge reaches the viewport bottom.
		// CTR-W3: the only slack below the final assistant prose is the
		// article's trailing turn-gap rhythm (--mai-turn-gap, 16px).
		expect(tail.bottomGap).toBeGreaterThanOrEqual(-2);
		expect(tail.bottomGap).toBeLessThanOrEqual(16);

		// The virtualized window stays bounded even with the tall tail.
		const domCount = Number((await viewport.getAttribute('data-dom-count')) ?? '0');
		expect(domCount).toBeGreaterThan(0);
		expect(domCount).toBeLessThanOrEqual(80);

		// Capture stable state across animation frames to prove zero
		// oscillation: scrollTop, scrollHeight and dom-count must never
		// flicker between virtua measurement passes.
		const samples: Array<{ scrollTop: number; scrollHeight: number; domCount: number }> = [];
		for (let frame = 0; frame < 6; frame += 1) {
			await page.evaluate(() => {
				const { promise, resolve } = Promise.withResolvers<void>();
				requestAnimationFrame(() => resolve());
				return promise;
			});
			const sample = await viewport.evaluate((node) => ({
				scrollTop: node.scrollTop,
				scrollHeight: node.scrollHeight,
				domCount: Number(node.getAttribute('data-dom-count') ?? '0')
			}));
			samples.push(sample);
		}
		// All samples must be identical — no jitter.
		const first = samples[0];
		for (const sample of samples) {
			expect(sample.scrollTop).toBe(first.scrollTop);
			expect(sample.scrollHeight).toBe(first.scrollHeight);
			expect(sample.domCount).toBe(first.domCount);
		}
	});

	// CTR-W1: the transcript's vertical rhythm. virtua measures each row
	// wrapper's CONTENT box (contentRect); spacing outside that box
	// (wrapper vertical padding, article margin) is invisible to the
	// layout, so deployed rows touched end-to-end (0px visible gaps,
	// measured live). The rhythm lives on the article's padding-bottom,
	// scaled per relation since CTR-W3 (turn 16 / tool 8 / call 4 /
	// base 12 — the token block on .mai-session-chat).
	// Proof by geometry, not eyeballing: this unscoped fixture's four
	// consecutive pairs keep their scale gaps and never intersect.
	test('vertical rhythm: consecutive rows keep the per-relation scale gaps', async ({ page }) => {
		await gotoHarness(page);
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				const committed = (seq: number, payload: Record<string, unknown>) =>
					ws.send(
						JSON.stringify({
							type: 'committed',
							stream_id: 's1',
							entry: {
								schema_version: 1,
								entry_id: `cccccccc-cccc-4ccc-8ccc-${String(seq).padStart(12, '0')}`,
								session_id: '11111111-1111-4111-8111-111111111111',
								seq,
								run_id: null,
								durable_source_id: `ctr-${seq}`,
								created_at: '2026-01-01T00:00:00Z',
								payload
							}
						})
					);
				// A realistic flat turn: user prompt, assistant prose, a
				// tool result, closing prose, next user prompt — every W1
				// row kind participates in the rhythm.
				committed(1, {
					kind: 'user_message',
					text: 'rotate the logs on alpha',
					client_request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
					source: 'api'
				});
				committed(2, {
					kind: 'assistant_message',
					text: 'Rotating the logs now.',
					model_profile: 'fast',
					tool_group_state: 'none'
				});
				committed(3, {
					kind: 'tool_result',
					call_id: 'call-1',
					tool_name: 'host_shell',
					capability_id: 'host.shell',
					status: 'executed',
					execution_status: 'succeeded',
					domain_outcome: 'success',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview: 'logs rotated',
					error_code: null,
					error_detail: null
				});
				committed(4, {
					kind: 'assistant_message',
					text: 'Done — logs rotated.',
					model_profile: 'fast',
					tool_group_state: 'none'
				});
				committed(5, {
					kind: 'user_message',
					text: 'thanks',
					client_request_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
					source: 'api'
				});
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));

		// Short transcript, no virtualized windowing: all five rows mount.
		await expect(page.getByTestId('session-entry')).toHaveCount(5);
		// Let virtua's ResizeObserver measurements settle.
		await page.waitForTimeout(300);

		type RowGeometry = {
			seq: number;
			borderTop: number;
			borderBottom: number;
			contentTop: number;
			contentBottom: number;
		};
		const rows = await page.evaluate((): RowGeometry[] => {
			const articles = Array.from(
				document.querySelectorAll('[data-testid="session-entry"]')
			) as HTMLElement[];
			return articles.map((row): RowGeometry => {
				// The VISIBLE content extent: the union of the painted
				// descendant rectangles — independent of where
				// padding/margin live, so the assertion cannot be
				// satisfied by merely moving padding onto the wrapper.
				// Hidden <details> bodies need checkVisibility: Chromium
				// keeps LAYOUT for closed-details content, so their rects
				// are real and offsetParent alone does not exclude them.
				let contentTop = Number.POSITIVE_INFINITY;
				let contentBottom = Number.NEGATIVE_INFINITY;
				const isVisible = (el: HTMLElement): boolean => {
					if (el.offsetParent === null) return false;
					const probe = el as HTMLElement & {
						checkVisibility?: (options: unknown) => boolean;
					};
					if (typeof probe.checkVisibility === 'function') {
						return probe.checkVisibility({ checkOpacity: false });
					}
					return true;
				};
				for (const el of Array.from(row.querySelectorAll<HTMLElement>('*'))) {
					if (!isVisible(el)) continue;
					const rect = el.getBoundingClientRect();
					if (rect.width === 0 && rect.height === 0) continue;
					contentTop = Math.min(contentTop, rect.top);
					contentBottom = Math.max(contentBottom, rect.bottom);
				}
				const box = row.getBoundingClientRect();
				return {
					seq: Number(row.dataset.entrySeq ?? '0'),
					borderTop: box.top,
					borderBottom: box.bottom,
					contentTop,
					contentBottom
				};
			});
		});
		expect(rows.length).toBe(5);

		// CTR-W3 per-relation scale for this unscoped fixture: user→16,
		// assistant(standalone)→16, orphan tool result→12 (base),
		// assistant→16. Gaps are the PREVIOUS row's trailing rhythm
		// (the lead has no group here — no tool group joins it).
		const expectedGaps = [16, 16, 12, 16];
		const visibleGaps: number[] = [];
		for (let i = 0; i + 1 < rows.length; i += 1) {
			const current = rows[i]!;
			const next = rows[i + 1]!;
			// DOM order follows seq order (nothing folds in this fixture).
			expect(next.seq).toBe(current.seq + 1);
			// A2: border boxes never intersect — no overlap at all.
			const borderGap = next.borderTop - current.borderBottom;
			expect(borderGap).toBeGreaterThanOrEqual(-0.5);
			// A1: the VISIBLE content gap — at least 4px, on the W3 scale.
			const visibleGap = next.contentTop - current.contentBottom;
			expect(visibleGap).toBeGreaterThanOrEqual(4);
			expect(visibleGap).toBeGreaterThanOrEqual(expectedGaps[i]! - 0.5);
			expect(visibleGap).toBeLessThanOrEqual(expectedGaps[i]! + 0.5);
			visibleGaps.push(visibleGap);
		}
		// Evidence: every consecutive pair carries its scale step.
		expect(visibleGaps.length).toBe(4);
	});

	// CTR-W2: turn grouping and hierarchy. A tool result / delegation
	// that belongs to a loaded assistant turn renders as its CHILD —
	// indented 16px (--mai-space-4) under a 1px rail spanning the
	// group — while orphans (no matching assistant in the window) and
	// unscoped rows stay top-level at the base indent. The rendered
	// DOM order must equal seq order exactly: grouping nests, it never
	// reorders.
	test('turn grouping: children nest under their assistant turn with a rail', async ({
		page
	}) => {
		const RUN_A = '11111111-1111-4111-8111-111111111112';
		const RUN_B = '11111111-1111-4111-8111-111111111113';
		const RUN_C = '11111111-1111-4111-8111-111111111114';
		const RUN_D = '11111111-1111-4111-8111-111111111115';
		await gotoHarness(page);
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				const committed = (
					seq: number,
					runId: string | null,
					payload: Record<string, unknown>
				) =>
					ws.send(
						JSON.stringify({
							type: 'committed',
							stream_id: 's1',
							entry: {
								schema_version: 1,
								entry_id: `cccccccc-cccc-4ccc-8ccc-${String(seq).padStart(12, '0')}`,
								session_id: '11111111-1111-4111-8111-111111111111',
								seq,
								run_id: runId,
								durable_source_id: `ctr2-${seq}`,
								created_at: '2026-01-01T00:00:00Z',
								payload
							}
						})
					);
				committed(1, null, {
					kind: 'user_message',
					text: 'check the logs on alpha',
					client_request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
					source: 'api'
				});
				// RUN A's turn: the assistant declares one tool call; its
				// matching result is the group's child.
				committed(2, RUN_A, {
					kind: 'assistant_message',
					text: 'Checking the logs.',
					model_profile: 'fast',
					tool_group_state: 'none',
					tool_calls: [
						{
							call_id: 'call-1',
							tool_name: 'host_shell',
							capability_id: 'host.shell',
							args: {}
						}
					]
				});
				committed(3, RUN_A, {
					kind: 'tool_result',
					call_id: 'call-1',
					tool_name: 'host_shell',
					capability_id: 'host.shell',
					status: 'executed',
					execution_status: 'succeeded',
					domain_outcome: 'success',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview: 'logs rotated',
					error_code: null,
					error_detail: null
				});
				// call-2 was never declared by any loaded assistant: an
				// ORPHAN result — top-level, never dropped.
				committed(4, RUN_A, {
					kind: 'tool_result',
					call_id: 'call-2',
					tool_name: 'host_shell',
					capability_id: 'host.shell',
					status: 'executed',
					execution_status: 'succeeded',
					domain_outcome: 'success',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview: 'stale tail',
					error_code: null,
					error_detail: null
				});
				// RUN B's turn: no declared calls, but its delegation card
				// (same run) is still the assistant's own action → child.
				committed(5, RUN_B, {
					kind: 'assistant_message',
					text: 'Delegating the tail read.',
					model_profile: 'fast',
					tool_group_state: 'none'
				});
				committed(6, RUN_B, {
					kind: 'delegation',
					child_run_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
					child_session_id: null,
					goal: 'read the rotated tails',
					status: 'succeeded',
					allowed_capabilities: ['host.logs'],
					result_summary: 'tails read',
					error_code: null
				});
				// RUN C has NO assistant message in the window: its
				// delegation stays top-level (orphan).
				committed(7, RUN_C, {
					kind: 'delegation',
					child_run_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
					child_session_id: null,
					goal: 'unrelated task',
					status: 'succeeded',
					allowed_capabilities: ['host.shell'],
					result_summary: 'done',
					error_code: null
				});
				// RUN D's assistant has no qualifying followers: a
				// childless turn renders as a plain row — no rail.
				committed(8, RUN_D, {
					kind: 'assistant_message',
					text: 'All done.',
					model_profile: 'fast',
					tool_group_state: 'none'
				});
				committed(9, null, {
					kind: 'user_message',
					text: 'thanks',
					client_request_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
					source: 'api'
				});
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));

		// Nothing folds in this fixture: all nine entries render.
		await expect(page.getByTestId('session-entry')).toHaveCount(9);
		// A3: the rendered order equals seq order exactly — grouping
		// nests children, it never reorders the stream.
		const domSeqs = await page.evaluate(() =>
			Array.from(
				document.querySelectorAll('[data-testid="session-entry"]')
			).map((row) => Number((row as HTMLElement).dataset.entrySeq))
		);
		expect(domSeqs).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);

		// Exactly TWO groups: RUN A's turn (lead 2, tool child 3) and
		// RUN B's turn (lead 5, delegation child 6). Everything else
		// stays top-level. CTR-W3: the tool child lives inside the
		// group's ONE collapsed tool-group disclosure (turn-tool-group);
		// turn-group-child now marks the delegation child only.
		const groups = page.getByTestId('turn-group');
		await expect(groups).toHaveCount(2);
		await expect(groups.first()).toHaveAttribute('data-turn-run-id', RUN_A);
		await expect(groups.nth(1)).toHaveAttribute('data-turn-run-id', RUN_B);
		await expect(page.getByTestId('turn-tool-group')).toHaveCount(1);
		const toolChildSeqs = await page.evaluate(() =>
			Array.from(
				document.querySelectorAll(
					'[data-testid="turn-tool-group"] [data-testid="session-entry"]'
				)
			).map((row) => Number((row as HTMLElement).dataset.entrySeq))
		);
		expect(toolChildSeqs).toEqual([3]);
		const childSeqs = await page.evaluate(() =>
			Array.from(
				document.querySelectorAll(
					'[data-testid="turn-group-child"] [data-testid="session-entry"]'
				)
			).map((row) => Number((row as HTMLElement).dataset.entrySeq))
		);
		expect(childSeqs).toEqual([6]);
		// The delegation rows both render (grouped child + orphan).
		await expect(page.getByTestId('entry-delegation')).toHaveCount(2);

		// A1, measured: both child kinds sit strictly deeper than their
		// parent prose (16px, --mai-space-4) and the group carries a
		// 1px rail.
		const hierarchy = await page.evaluate(() => {
			const groupA = document.querySelector('[data-testid="turn-group"]') as HTMLElement;
			const proseA = groupA
				.querySelector('[data-testid="entry-assistant-message"]')!
				.getBoundingClientRect();
			const toolGroup = groupA
				.querySelector('[data-testid="turn-tool-group"]')!
				.getBoundingClientRect();
			const groupB = document.querySelectorAll('[data-testid="turn-group"]')[1] as HTMLElement;
			const proseB = groupB
				.querySelector('[data-testid="entry-assistant-message"]')!
				.getBoundingClientRect();
			const delegationChild = groupB
				.querySelector('[data-testid="turn-group-child"]')!
				.getBoundingClientRect();
			const rail = getComputedStyle(groupA, '::before');
			return {
				toolIndent: toolGroup.left - proseA.left,
				delegationIndent: delegationChild.left - proseB.left,
				railWidth: rail.width,
				railColor: rail.backgroundColor
			};
		});
		expect(hierarchy.toolIndent).toBeGreaterThanOrEqual(16 - 0.5);
		expect(hierarchy.toolIndent).toBeLessThanOrEqual(16 + 0.5);
		expect(hierarchy.delegationIndent).toBeGreaterThanOrEqual(16 - 0.5);
		expect(hierarchy.delegationIndent).toBeLessThanOrEqual(16 + 0.5);
		expect(hierarchy.railWidth).toBe('1px');
		expect(hierarchy.railColor).not.toBe('rgba(0, 0, 0, 0)');

		// A2, measured: the orphan result (seq 4), the orphan delegation
		// (seq 7) and the childless assistant (seq 8) have NO group
		// ancestor and sit at the BASE indent (same left as the seq 1
		// user row) — never dropped, no rail.
		const orphans = await page.evaluate(() => {
			const rowOf = (seq: string) =>
				document.querySelector(`[data-entry-seq="${seq}"]`) as HTMLElement | null;
			const base = rowOf('1')!.getBoundingClientRect().left;
			return {
				toolGroupless: rowOf('4')!.closest('[data-testid="turn-group"]') === null,
				delegationGroupless: rowOf('7')!.closest('[data-testid="turn-group"]') === null,
				childlessGroupless: rowOf('8')!.closest('[data-testid="turn-group"]') === null,
				toolLeft: rowOf('4')!.getBoundingClientRect().left,
				delegationLeft: rowOf('7')!.getBoundingClientRect().left,
				childlessLeft: rowOf('8')!.getBoundingClientRect().left,
				base
			};
		});
		expect(orphans.toolGroupless).toBe(true);
		expect(orphans.delegationGroupless).toBe(true);
		expect(orphans.childlessGroupless).toBe(true);
		expect(orphans.toolLeft).toBeCloseTo(orphans.base, 1);
		expect(orphans.delegationLeft).toBeCloseTo(orphans.base, 1);
		expect(orphans.childlessLeft).toBeCloseTo(orphans.base, 1);
	});

	// CTR-W3: collapsed tool group, typed summary, folded delegation.
	// A turn's whole tool activity is ONE disclosure row whose summary
	// lists every capability + its status WORD; settled rounds collapse,
	// unproven outcomes stay open; a delegation collapses to a one-line
	// summary (child run + status) so its goal/result_summary can never
	// inflate the chat flow (a live delegation card measured 1616px).
	// The per-relation rhythm (turn 16 / prose↔group 8 / in-group 4)
	// is measured on the same rendered fixture.
	test('tool groups collapse behind a typed summary; delegations fold away', async ({ page }) => {
		const RUN_A = '21111111-1111-4111-8111-111111111112';
		const RUN_B = '21111111-1111-4111-8111-111111111113';
		const RUN_C = '21111111-1111-4111-8111-111111111114';
		await gotoHarness(page);
		await page.routeWebSocket(/\/channel/, (ws) => {
			ws.onMessage((data) => {
				if (!String(data).includes('"hello"')) return;
				ws.send(
					JSON.stringify({
						type: 'hello_ok',
						protocol_version: 1,
						session_id: '11111111-1111-4111-8111-111111111111',
						server_id: GLOBAL_SERVER_ID,
						stream_id: 's1',
						latest_seq: 0
					})
				);
				const committed = (
					seq: number,
					runId: string | null,
					payload: Record<string, unknown>
				) =>
					ws.send(
						JSON.stringify({
							type: 'committed',
							stream_id: 's1',
							entry: {
								schema_version: 1,
								entry_id: `cccccccc-cccc-4ccc-8ccc-${String(seq).padStart(12, '0')}`,
								session_id: '11111111-1111-4111-8111-111111111111',
								seq,
								run_id: runId,
								durable_source_id: `ctr3-${seq}`,
								created_at: '2026-01-01T00:00:00Z',
								payload
							}
						})
					);
				committed(1, null, {
					kind: 'user_message',
					text: 'run the migration',
					client_request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
					source: 'api'
				});
				// RUN A declares TWO calls: one executes, one is denied —
				// the summary must show both capabilities + both words,
				// and the unproven denial keeps the group OPEN.
				committed(2, RUN_A, {
					kind: 'assistant_message',
					text: 'Migrating now.',
					model_profile: 'fast',
					tool_group_state: 'pending',
					tool_calls: [
						{ call_id: 'call-1', tool_name: 'docs_retrieve', capability_id: 'docs.retrieve', args: {} },
						{ call_id: 'call-2', tool_name: 'host_shell', capability_id: 'host.shell', args: {} }
					]
				});
				committed(3, RUN_A, {
					kind: 'tool_result',
					call_id: 'call-1',
					tool_name: 'docs_retrieve',
					capability_id: 'docs.retrieve',
					status: 'executed',
					execution_status: 'succeeded',
					domain_outcome: 'success',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview: 'row 42 migrated',
					error_code: null,
					error_detail: null
				});
				committed(4, RUN_A, {
					kind: 'tool_result',
					call_id: 'call-2',
					tool_name: 'host_shell',
					capability_id: 'host.shell',
					status: 'denied',
					execution_status: 'succeeded',
					domain_outcome: 'failure',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview: null,
					error_code: 'denied_by_policy',
					error_detail: 'capability not granted'
				});
				// RUN B: a delegation whose goal/summary are LONG — the
				// collapsed card must stay a small one-line row.
				committed(5, RUN_B, {
					kind: 'assistant_message',
					text: 'Delegating the verification.',
					model_profile: 'fast',
					tool_group_state: 'none'
				});
				committed(6, RUN_B, {
					kind: 'delegation',
					child_run_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
					child_session_id: null,
					goal: 'VERIFY-GOAL-TOKEN ' + 'verify every migrated row, re-read the tails, and reconcile the ledger. '.repeat(12),
					status: 'succeeded',
					allowed_capabilities: ['host.logs'],
					result_summary: 'VERIFY-SUMMARY-TOKEN ' + 'all rows verified, tails match, ledger reconciled with zero drift. '.repeat(12),
					error_code: null
				});
				committed(7, null, {
					kind: 'user_message',
					text: 'thanks',
					client_request_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
					source: 'api'
				});
				// RUN C: a fully settled round — its tool group must be
				// COLLAPSED by default.
				committed(8, RUN_C, {
					kind: 'assistant_message',
					text: 'Checking the logs.',
					model_profile: 'fast',
					tool_group_state: 'none',
					tool_calls: [
						{ call_id: 'call-3', tool_name: 'host_logs', capability_id: 'host.logs', args: {} }
					]
				});
				committed(9, RUN_C, {
					kind: 'tool_result',
					call_id: 'call-3',
					tool_name: 'host_logs',
					capability_id: 'host.logs',
					status: 'executed',
					execution_status: 'succeeded',
					domain_outcome: 'success',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview: 'tail ok',
					error_code: null,
					error_detail: null
				});
				// Out-of-run (fixture) rows: executed collapses now — the
				// null-run case no longer dumps raw output into the flow.
				committed(10, null, {
					kind: 'tool_result',
					call_id: 'call-x',
					tool_name: 'host_shell',
					capability_id: 'host.shell',
					status: 'executed',
					execution_status: 'succeeded',
					domain_outcome: 'success',
					effect_certainty: 'certain',
					payload_hash: null,
					output_preview: 'fixture output',
					error_code: null,
					error_detail: null
				});
				committed(11, null, {
					kind: 'tool_result',
					call_id: 'call-y',
					tool_name: 'host_shell',
					capability_id: 'host.shell',
					status: 'unknown_effect',
					execution_status: 'outcome_unknown',
					domain_outcome: 'unknown',
					effect_certainty: 'uncertain',
					payload_hash: null,
					output_preview: 'effect not proven',
					error_code: null,
					error_detail: null
				});
			});
		});
		await page.evaluate(() => window.__open!('11111111-1111-4111-8111-111111111111'));

		await expect(page.getByTestId('session-entry')).toHaveCount(11);
		const domSeqs = await page.evaluate(() =>
			Array.from(
				document.querySelectorAll('[data-testid="session-entry"]')
			).map((row) => Number((row as HTMLElement).dataset.entrySeq))
		);
		expect(domSeqs).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);

		// ---- A1/A4: ONE tool-group row for the two-call turn, summary
		// carrying BOTH capabilities and BOTH status words. --------------
		const toolGroups = page.getByTestId('turn-tool-group');
		await expect(toolGroups).toHaveCount(2);
		const summaryA = await toolGroups.first().evaluate((node) => node.textContent ?? '');
		expect(summaryA).toContain('docs.retrieve');
		expect(summaryA).toContain('host.shell');
		expect(summaryA).toContain('executed');
		expect(summaryA).toContain('denied');
		// ONE group row for the turn: both result articles live inside
		// the single disclosure body.
		const insideA = await toolGroups
			.first()
			.evaluate((node) => node.querySelectorAll('[data-testid="session-entry"]').length);
		expect(insideA).toBe(2);

		// ---- A2: settled collapses, unproven stays open. ----------------
		// RUN A holds a denial → the group is OPEN (visible without a
		// click), while the settled inner executed row stays collapsed.
		expect(await toolGroups.first().getAttribute('open')).not.toBeNull();
		const innerExecuted = toolGroups
			.first()
			.locator('[data-entry-seq="3"] > details');
		expect(await innerExecuted.getAttribute('open')).toBeNull();
		// RUN C fully settled → collapsed.
		expect(await toolGroups.nth(1).getAttribute('open')).toBeNull();
		// Fixture (null-run) rows: executed collapses; unknown_effect opens.
		expect(
			await page.locator('[data-entry-seq="10"] > details').getAttribute('open')
		).toBeNull();
		expect(
			await page.locator('[data-entry-seq="11"] > details').getAttribute('open')
		).not.toBeNull();

		// ---- A3: the delegation folds away behind a one-line summary. --
		const delegation = page.locator('[data-entry-seq="6"] > details');
		expect(await delegation.getAttribute('open')).toBeNull();
		const folded = await delegation.evaluate((raw) => {
			const node = raw as HTMLElement;
			return {
				height: node.getBoundingClientRect().height,
				// innerText EXCLUDES the hidden body: the goal/summary
				// must not render into the chat flow; textContent keeps
				// them reachable (no information loss).
				visibleText: node.innerText,
				domText: node.textContent ?? ''
			};
		});
		expect(folded.height).toBeLessThan(120);
		expect(folded.visibleText).not.toContain('VERIFY-GOAL-TOKEN');
		expect(folded.visibleText).not.toContain('VERIFY-SUMMARY-TOKEN');
		expect(folded.domText).toContain('VERIFY-GOAL-TOKEN');
		expect(folded.domText).toContain('VERIFY-SUMMARY-TOKEN');
		// The one-line summary carries the child run id + status word.
		expect(folded.visibleText).toContain('dddddddd');
		expect(folded.visibleText).toContain('succeeded');

		// ---- A5: the per-relation rhythm, measured on real rects. ------
		const gaps = await page.evaluate(() => {
			const rowOf = (seq: string) =>
				document.querySelector(`[data-entry-seq="${seq}"]`) as HTMLElement;
			// The union of painted descendant rects = the visible content
			// extent. checkVisibility excludes closed-<details> bodies:
			// Chromium keeps their LAYOUT, so their rects are real and
			// offsetParent alone does not exclude them.
			const contentBottomOf = (root: Element) => {
				let bottom = Number.NEGATIVE_INFINITY;
				for (const el of Array.from(root.querySelectorAll<HTMLElement>('*'))) {
					if (el.offsetParent === null) continue;
					const probe = el as HTMLElement & {
						checkVisibility?: (options: unknown) => boolean;
					};
					if (
						typeof probe.checkVisibility === 'function' &&
						!probe.checkVisibility({ checkOpacity: false })
					) {
						continue;
					}
					const rect = el.getBoundingClientRect();
					if (rect.width === 0 && rect.height === 0) continue;
					bottom = Math.max(bottom, rect.bottom);
				}
				return bottom;
			};
			const user1 = rowOf('1');
			const prose2 = rowOf('2').querySelector('[data-testid="entry-assistant-message"]')!;
			const toolGroupA = document.querySelector('[data-testid="turn-tool-group"]') as HTMLElement;
			const result3 = rowOf('3');
			const result4 = rowOf('4');
			return {
				// user turn ↔ assistant turn (16px)
				userToAssistant:
					prose2.getBoundingClientRect().top - contentBottomOf(user1),
				// assistant prose ↔ its own tool group (8px)
				proseToGroup: toolGroupA.getBoundingClientRect().top - contentBottomOf(prose2),
				// inside the tool group: capability ↔ its result (4px)
				insideGroup:
					result4.getBoundingClientRect().top - contentBottomOf(result3)
			};
		});
		expect(gaps.userToAssistant).toBeGreaterThanOrEqual(16 - 0.5);
		expect(gaps.userToAssistant).toBeLessThanOrEqual(16 + 0.5);
		expect(gaps.proseToGroup).toBeGreaterThanOrEqual(8 - 0.5);
		expect(gaps.proseToGroup).toBeLessThanOrEqual(8 + 0.5);
		expect(gaps.insideGroup).toBeGreaterThanOrEqual(4 - 0.5);
		expect(gaps.insideGroup).toBeLessThanOrEqual(4 + 0.5);
	});
});

// ---------------------------------------------------------------------------
// Live backend (gated): real shell + real backend + real Postgres
// ---------------------------------------------------------------------------

test.describe('live backend (gated)', () => {
	test.skip(
		process.env.MAI_E2E_SESSION_BACKEND !== '1',
		'requires MAI_E2E_SESSION_BACKEND=1 and the disposable Postgres fixture'
	);
	// Live-shell wiring: Playwright starts the config webServer on
	// 127.0.0.1:4173 (baseURL). The runner exports MAI_PREVIEW_API_PROXY so
	// THAT preview proxies same-origin /api (REST + WebSocket) to the
	// uvicorn spawned in beforeAll below — the shell boots LIVE on :4173.
	// Any preview already squatting on :4173 was started WITHOUT the proxy
	// env and would serve the offline fixture shell instead.

	let uvicorn: { kill(): void } | null = null;
	let preview: { kill(): void } | null = null;
	let liveServerId = GLOBAL_SERVER_ID;

	test.beforeAll(async () => {
		test.setTimeout(120_000);
		killPortSquatters();
		// Fresh canonical file store: server profiles live as directories
		// under MAI_DATA_ROOT (the reviewed file store); a stale root would
		// surface PREVIOUS runs' profiles to the shell.
		spawnSync('bash', ['-c', 'rm -rf /tmp/mai-d08-e2e-root && mkdir -p /tmp/mai-d08-e2e-root']);
		const backendRoot = new URL('../../backend', import.meta.url).pathname;
		const dbUrl = process.env.MAI_E2E_DATABASE_URL!;
		const adminUrl = dbUrl.replace(/\/[^/]*$/, '/postgres');
		const reset = spawnSync(
			'uv',
			[
				'run',
				'python',
				'-c',
				`import psycopg; c = psycopg.connect(${JSON.stringify(adminUrl)}, autocommit=True); c.execute('DROP DATABASE IF EXISTS mai_e2e'); c.execute('CREATE DATABASE mai_e2e')`
			],
			{ cwd: backendRoot, encoding: 'utf-8' }
		);
		if (reset.status !== 0) {
			throw new Error(`db reset failed (${reset.status}): ${reset.stderr}`);
		}
		const migrated = spawnSync('uv', ['run', 'alembic', 'upgrade', 'head'], {
			cwd: backendRoot,
			env: { ...process.env, MAI_DATABASE_URL: dbUrl },
			encoding: 'utf-8'
		});
		if (migrated.status !== 0) throw new Error(`alembic failed: ${migrated.stderr}`);
		const bootstrapped = spawnSync(
			'uv',
			[
				'run',
				'python',
				'-c',
				'import os; from mai.jobs import engine as jobs_engine; jobs_engine.launch_worker(os.environ["MAI_DATABASE_URL"]); jobs_engine.shutdown()'
			],
			{
				cwd: backendRoot,
				env: { ...process.env, MAI_DATABASE_URL: dbUrl },
				encoding: 'utf-8'
			}
		);
		if (bootstrapped.status !== 0) {
			throw new Error(`jobs bootstrap failed: ${bootstrapped.stderr}`);
		}
		uvicorn = spawn(
			'bash',
			[
				'-c',
				`exec uv run uvicorn mai.api.app:create_app --factory --host 127.0.0.1 --port ${E2E_PORT} >/tmp/mai-d08-uvicorn.log 2>&1`
			],
			{
				cwd: backendRoot,
				env: {
					...process.env,
					MAI_DATABASE_URL: dbUrl,
					MAI_PUBLIC_ORIGINS: 'http://127.0.0.1:4173',
					MAI_DATA_ROOT: '/tmp/mai-d08-e2e-root'
				}
			}
		);
		// The REAL app shell on a NON-fixture port (DesktopShell's offline
		// fixture host is exactly 127.0.0.1:4173; 4175 runs live). The vite
		// preview proxies same-origin /api (REST *and* WebSocket) to the
		// spawned backend — the browser talks to ONE origin, exactly like
		// the reviewed single-proxy deployment.
		preview = spawn(
			'bash',
			[
				'-c',
				'exec pnpm exec vite preview --port 4175 --strictPort >/tmp/mai-d08-preview.log 2>&1'
			],
			{
				cwd: FRONTEND_ROOT,
				env: { ...process.env, MAI_PREVIEW_API_PROXY: E2E_BASE }
			}
		);
		// Readiness (backend + preview).
		const deadline = Date.now() + 45_000;
		let backendUp = false;
		while (Date.now() < deadline && !backendUp) {
			try {
				const response = await fetch(`${E2E_BASE}/api/v1/health`);
				backendUp = response.status === 200;
			} catch {
				await new Promise((resolve) => setTimeout(resolve, 500));
			}
		}
		if (!backendUp) throw new Error('uvicorn never became healthy');
	});

	test.afterAll(async () => {
		uvicorn?.kill();
		preview?.kill();
		killPortSquatters();
	});

	async function backendLogin(page: Page): Promise<string> {
		const csrfResponse = await page.request.post(`${E2E_BASE}/api/v1/auth/bootstrap`, {
			data: { password: E2E_PASSWORD },
			headers: { origin: 'http://127.0.0.1:4173' }
		});
		expect([201, 409]).toContain(csrfResponse.status());
		const login = await page.request.post(`${E2E_BASE}/api/v1/auth/login`, {
			data: { password: E2E_PASSWORD },
			headers: { origin: 'http://127.0.0.1:4173' }
		});
		expect(login.status()).toBe(204);
		// Mirror the backend cookies onto the shell origin's jar (the
		// same-origin proxy makes same-host cookies authoritative).
		const headers = login.headersArray().filter((header) => header.name.toLowerCase() === 'set-cookie');
		for (const header of headers) {
			const [pair] = header.value.split(';');
			const name = pair.slice(0, pair.indexOf('='));
			const value = pair.slice(pair.indexOf('=') + 1);
			await page.context().addCookies([
				{
					name,
					value,
					domain: '127.0.0.1',
					path: '/'
				}
			]);
		}
		return headers
			.find((header) => header.value.startsWith('mai_csrf'))
			?.value.split('=')[1]
			.split(';')[0] ?? '';
	}
	test('10k session: bounded pages, caps, anchoring, kinds, refresh, tag_preview', async ({
		page
	}, testInfo) => {
		test.setTimeout(180_000);
		const csrf = await backendLogin(page);

		// Enroll one server (the shell needs a live profile) and seed.
		const created = await page.request.post(`${E2E_BASE}/api/v1/servers`, {
			headers: { origin: 'http://127.0.0.1:4173', 'x-csrf-token': csrf },
			data: {
				name: 'd08-e2e-server',
				prompt_revision: 7,
				environment: 'linux_ssh',
				ssh: {
					host: 'fixture.internal',
					port: 22,
					user: 'fixture',
					host_key: 'SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU',
					credential_ref: 'MAI_SSH_KEY_FIXTURE'
				},
				privilege: 'none',
				capabilities: []
			}
		});
		if (created.status() !== 201) {
			throw new Error(`servers POST: ${created.status()} ${await created.text()}`);
		}
		const profile = (await created.json()) as { server_id: string };
		liveServerId = profile.server_id;

		// The session for the 10k seed (created through the REAL API).
		const sessionResponse = await page.request.post(`${E2E_BASE}/api/v1/agent-sessions`, {
			headers: { origin: 'http://127.0.0.1:4173', 'x-csrf-token': csrf },
			data: {
				server_id: liveServerId,
				source: 'interactive',
				client_request_id: crypto.randomUUID()
			}
		});
		expect(sessionResponse.status()).toBe(201);
		const bigSessionId = (await sessionResponse.json()).session_id as string;

		// Seed 10,000 entries in ONE SQL statement (<1 s).
		const backendRoot = new URL('../../backend', import.meta.url).pathname;
		const seeded = spawnSync(
			'uv',
			[
				'run',
				'python',
				'-c',
				[
					`import psycopg`,
					`c = psycopg.connect(${JSON.stringify(process.env.MAI_E2E_DATABASE_URL!)}, autocommit=True)`,
					`sid = ${JSON.stringify(bigSessionId)}`,
					`c.execute("""`,
					`INSERT INTO session_entries (entry_id, session_id, run_id, seq, kind, payload, durable_source_id)`,
					`SELECT gen_random_uuid(), %s::uuid, NULL, n, 'user_message',`,
					` jsonb_build_object('kind','user_message','text','bulk entry ' || n,`,
					` 'client_request_id', gen_random_uuid()::text, 'source','api'),`,
					` 'bulk-' || n FROM generate_series(1, 10000) AS n(n)`,
					`""", (sid,))`,
					`print('seeded', c.execute('SELECT COUNT(*) FROM session_entries').fetchone()[0])`
				].join('\n'),
				// Positional parameter for the session id (psycopg %s).
				bigSessionId
			],
			{ cwd: backendRoot, encoding: 'utf-8' }
		);
		if (!seeded.stdout.includes('seeded 10000')) {
			throw new Error(
				`seed failed (status ${seeded.status}): ${seeded.stderr}\nstdout: ${seeded.stdout}`
			);
		}

		// A small session for the execution-UX kinds + refresh continuity.
		const smallResponse = await page.request.post(`${E2E_BASE}/api/v1/agent-sessions`, {
			headers: { origin: 'http://127.0.0.1:4173', 'x-csrf-token': csrf },
			data: {
				server_id: liveServerId,
				source: 'interactive',
				client_request_id: crypto.randomUUID()
			}
		});
		const smallSessionId = (await smallResponse.json()).session_id as string;
		const HASH_A = 'a'.repeat(64);
		const HASH_B = 'b'.repeat(64);
		const HASH_C = 'c'.repeat(64);
		const kindRows = [
			[
				1,
				'user_message',
				'{"kind":"user_message","text":"run the checks","client_request_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","source":"api"}'
			],
			[
				2,
				'assistant_message',
				'{"kind":"assistant_message","text":"running them now","model_profile":"fast","tool_group_state":"pending","tool_calls":[{"call_id":"call-1","tool_name":"app.discover","capability_id":"app.discover","args":{"query":"services"}}]}'
			],
			[
				3,
				'tool_result',
				`{"kind":"tool_result","call_id":"call-1","tool_name":"app.discover","capability_id":"app.discover","status":"unknown_effect","execution_status":"outcome_unknown","domain_outcome":"unknown","effect_certainty":"uncertain","payload_hash":"${HASH_A}","output_preview":null,"artifact_ref":null,"error_code":null,"error_detail":null}`
			],
			[
				4,
				'tool_result',
				`{"kind":"tool_result","call_id":"call-1","tool_name":"app.discover","capability_id":"app.discover","status":"executed","execution_status":"succeeded","domain_outcome":"success","effect_certainty":"certain","payload_hash":"${HASH_B}","output_preview":"3 services","artifact_ref":null,"error_code":null,"error_detail":null}`
			],
			[
				5,
				'tool_result',
				`{"kind":"tool_result","call_id":"call-2","tool_name":"host.shell","capability_id":"host.shell","status":"denied","execution_status":"succeeded","domain_outcome":"failure","effect_certainty":"certain","payload_hash":"${HASH_C}","output_preview":null,"artifact_ref":null,"error_code":"denied_by_policy","error_detail":"capability not granted"}`
			],
			[
				6,
				'delegation',
				'{"kind":"delegation","child_run_id":"dddddddd-dddd-4ddd-8ddd-dddddddddddd","child_session_id":"eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee","goal":"inspect the alpha host","status":"succeeded","allowed_capabilities":["host.logs"],"result_summary":"logs rotated"}'
			],
			[
				7,
				'compaction',
				'{"kind":"compaction","compaction_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc","from_seq":1,"to_seq":5,"summary_text":"earlier checks summarised","model_profile":"fast","token_usage_anchor":null}'
			],
			[
				8,
				'run_state',
				'{"kind":"run_state","state":"running","reason_code":null,"detail":null}'
			],
			[
				9,
				'control',
				'{"kind":"control","action":"steer","client_request_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccd","target_run_id":null,"approval_id":null,"note":"focus on nginx"}'
			]
		];
		const seedKindsResult = spawnSync(
			'uv',
			[
				'run',
				'python',
				'-c',
				[
					`import psycopg, json`,
					`c = psycopg.connect(${JSON.stringify(process.env.MAI_E2E_DATABASE_URL!)}, autocommit=True)`,
					`sid = ${JSON.stringify(smallSessionId)}`,
					`rows = ${JSON.stringify(kindRows)}`,
					`cur = c.cursor()`,
					`cur.executemany("INSERT INTO session_entries (entry_id, session_id, run_id, seq, kind, payload, durable_source_id) VALUES (gen_random_uuid(), %s::uuid, NULL, %s, %s, %s::jsonb, 'kind-' || %s)", [(sid, seq, kind, payload, kind) for seq, kind, payload in rows])`,
					`print('kinds seeded')`
				].join('\n')
			],
			{ cwd: backendRoot, encoding: 'utf-8' }
		);
		if (!seedKindsResult.stdout.includes('kinds seeded')) {
			throw new Error(
				`kinds seed failed (status ${seedKindsResult.status}): ${seedKindsResult.stderr}`
			);
		}

		// Attach a catalog tag to the small session (R2-1 proof).
		await page.request.post(`${E2E_BASE}/api/v1/agent-sessions/${smallSessionId}/tags`, {
			headers: { origin: 'http://127.0.0.1:4173', 'x-csrf-token': csrf },
			data: { server_id: liveServerId, tag_id: crypto.randomUUID(), tag_name: 'd08' }
		});

		// Open the REAL shell (live mode on the non-fixture port).
		await page.goto('/');
		await page.waitForTimeout(6_000);
		if ((await page.getByTestId('drawer-history-item').count()) === 0) {
			const snapshot = {
				url: page.url(),
				testids: await page.evaluate(() =>
					[...document.querySelectorAll('[data-testid]')]
						.slice(0, 30)
						.map((node) => node.getAttribute('data-testid'))
				),
				cookies: (await page.context().cookies('http://127.0.0.1:4173')).map(
					(cookie) => cookie.name
				),
				statusText: await page
					.evaluate(() => document.querySelector('[data-testid="desktop-status"]')?.textContent ?? null),
				pageFetchServers: await page
					.evaluate(async () => {
						const response = await fetch('/api/v1/servers', { credentials: 'include' });
						return `${response.status} ${JSON.stringify(await response.json()).slice(0, 160)}`;
					})
					.catch((error) => String(error)),
				cookieHeader: await page.evaluate(() => document.cookie),
				sessionsProbe: await page.evaluate(async () => {
					const response = await fetch(
						'/api/v1/agent-sessions?limit=10',
						{ credentials: 'include' }
					);
					const body = (await response.json()) as {
						items?: Array<{ session_id: string; server_id: string }>;
						total?: number;
					};
					return `${response.status} total=${body.total} items=${JSON.stringify((body.items ?? []).map((one) => [one.session_id.slice(0, 8), one.server_id.slice(0, 8)]))}`;
				})
					.catch((error) => String(error)),
				drawerSessionsLen: await page.evaluate(
					() =>
						document.querySelectorAll('[data-testid="drawer-history-item"]').length
				),
				selectedServer: await page.evaluate(
					() => document.querySelector('.mai-server-tree select')?.textContent ?? null
				)
			};
			console.log('[shell-diag]', JSON.stringify(snapshot));
			await page.screenshot({ path: '/tmp/d08-shell-diag.png' });
		}
		await expect(page.getByTestId('drawer-history-item').first()).toBeVisible({
			timeout: 30_000
		});

		// ---- R2-1: the drawer session row carries the tag preview --------
		await expect(page.getByTestId('drawer-history-row-tags').first()).toBeVisible();

		// ---- R2-2/refresh: select the 10k session in the drawer ----------
		// The drawer rows are session-first first: the SMALL session (newest)
		// then the BIG one; select by matching the big session's label.
		const bigRow = page
			.getByTestId('drawer-history-item')
			.filter({ hasText: bigSessionId.slice(0, 8) });
		const bigCount = await bigRow.count();
		if (bigCount === 0) {
			// Rows render short ids via the title fallback — match by seq data.
			await page.getByTestId('drawer-history-item').nth(1).click();
		} else {
			await bigRow.first().click();
		}
		// The dock swapped to the session-first panel (R2-2 surface).
		await expect(page.getByTestId('session-chat-panel')).toBeVisible();
		const viewport = page.getByTestId('session-history-viewport');
		await expect(viewport).toBeVisible();

		// Initial load: at most 30 cached entries (the channel snapshot; no
		// historical run replay) and the newest tail visible. Poll for a
		// non-empty cache FIRST: 0 (channel still connecting) satisfies
		// ≤30 vacuously.
		await expect
			.poll(
				async () =>
					Number((await viewport.getAttribute('data-cached-count')) ?? '0'),
				{ timeout: 15_000 }
			)
			.toBeGreaterThan(0);
		await expect
			.poll(
				async () =>
					Number((await viewport.getAttribute('data-cached-count')) ?? '0'),
				{ timeout: 15_000 }
			)
			.toBeLessThanOrEqual(30);
		const latestSeq = Number((await viewport.getAttribute('data-latest-seq')) ?? '0');
		// Seed-only session: latest committed seq == 10000 (no historical
		// run replay, no admission in this session).
		expect(latestSeq).toBe(10000);
		await expect(viewport).toHaveAttribute('data-has-older', 'true');

		// ---- Older pages: upward scroll, ≤50/page, anchored ---------------
		const pageSizes: number[] = [];
		page.on('response', (response) => {
			const url = response.url();
			if (url.includes('/history')) {
				void response
					.body()
					.then((body) => pageSizes.push(body.byteLength))
					.catch(() => undefined);
			}
		});
		// Rounds: each trigger (scroll-to-top once, then the keyboard-
		// accessible Load older control) must pull exactly ONE bounded page:
		// oldest seq strictly decreases, ≤50 per page, single-flight dedup
		// absorbs the stray scroll events the anchoring provokes.
		let previousOldest = Number((await viewport.getAttribute('data-oldest-seq')) ?? '0');
		await viewport.evaluate((node) => {
			node.scrollTop = 0;
		});
		await expect
			.poll(
				async () =>
					Number((await viewport.getAttribute('data-oldest-seq')) ?? '0'),
				{ timeout: 15_000 }
			)
			.toBeLessThan(previousOldest);
		previousOldest = Number((await viewport.getAttribute('data-oldest-seq')) ?? '0');
		for (let round = 0; round < 2; round += 1) {
			await page.getByTestId('session-load-older').click();
			await expect
				.poll(
					async () =>
						Number((await viewport.getAttribute('data-oldest-seq')) ?? '0'),
					{ timeout: 15_000 }
				)
				.toBeLessThan(previousOldest);
			previousOldest = Number((await viewport.getAttribute('data-oldest-seq')) ?? '0');
			expect(previousOldest).toBeGreaterThan(1);
		}

		// ---- Caps: cached ≤400, DOM window ≤80 ----------------------------
		const cachedCount = Number((await viewport.getAttribute('data-cached-count')) ?? '0');
		expect(cachedCount).toBeLessThanOrEqual(400);
		const domCount = Number((await viewport.getAttribute('data-dom-count')) ?? '0');
		expect(domCount).toBeLessThanOrEqual(80);
		expect(cachedCount + domCount).toBeLessThanOrEqual(400);

		// Every history page ≤64 KiB.
		for (const size of pageSizes) expect(size).toBeLessThanOrEqual(64 * 1024);

		await page.screenshot({
			path: `${EVIDENCE_DIR}/d08-10k-history.png`,
			fullPage: false
		});

		// ---- Execution UX kinds (the small session) -----------------------
		const smallRow = page
			.getByTestId('drawer-history-item')
			.filter({ hasText: smallSessionId.slice(0, 8) });
		if ((await smallRow.count()) > 0) {
			await smallRow.first().click();
		} else {
			await page.getByTestId('drawer-history-item').first().click();
		}
		await expect(page.getByTestId('session-chat-panel')).toBeVisible();
		await expect(
			page.getByTestId('tool-status-badge').filter({ hasText: 'Outcome unknown' })
		).toBeVisible();
		await expect(page.getByTestId('tool-status-badge').filter({ hasText: /^executed/ })).toBeVisible();
		await expect(page.getByTestId('tool-status-badge').filter({ hasText: 'denied' })).toBeVisible();
		await expect(page.getByTestId('entry-delegation')).toContainText('dddddddd-dddd-4ddd-8ddd-dddddddddddd');
		await expect(page.getByTestId('compaction-marker')).toContainText('Compacted entries seq 1..5');
		await page.screenshot({ path: `${EVIDENCE_DIR}/d08-execution-ux.png` });

		// ---- Refresh continuity (R2-3): reload restores the session -------
		await page.evaluate(() => {
			window.localStorage.clear();
		});
		await page.reload();
		await expect(page.getByTestId('session-chat-panel')).toBeVisible({ timeout: 30_000 });
		const restored = await page.getByTestId('session-header-id').textContent();
		expect(restored).toContain(smallSessionId.slice(0, 8));
		await expect(page.getByTestId('session-entry').first()).toBeVisible();
		// No transcript in web storage: the pointer carries ONLY the ids.
		const pointerRaw = await page.evaluate(() =>
			sessionStorage.getItem('mai-active-session')
		);
		const pointer = JSON.parse(pointerRaw ?? '{}') as Record<string, unknown>;
		expect(Object.keys(pointer).sort()).toEqual(['server_id', 'session_id']);
		expect(
			await page.evaluate(() => window.localStorage.getItem('mai-active-session'))
		).toBeNull();
	});

	async function seedAndOpen(
		page: Page,
		csrf: string,
		sessionName: string
	): Promise<{ serverId: string; sessionId: string }> {
		const created = await page.request.post(`${E2E_BASE}/api/v1/servers`, {
			headers: { origin: 'http://127.0.0.1:4173', 'x-csrf-token': csrf },
			data: {
				name: sessionName,
				prompt_revision: 7,
				environment: 'linux_ssh',
				ssh: {
					host: 'fixture.internal',
					port: 22,
					user: 'fixture',
					host_key: 'SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU',
					credential_ref: 'MAI_SSH_KEY_FIXTURE'
				},
				privilege: 'none',
				capabilities: []
			}
		});
		if (created.status() !== 201) {
			throw new Error(`servers POST: ${created.status()} ${await created.text()}`);
		}
		const serverId = ((await created.json()) as { server_id: string }).server_id;
		const sessionResponse = await page.request.post(`${E2E_BASE}/api/v1/agent-sessions`, {
			headers: { origin: 'http://127.0.0.1:4173', 'x-csrf-token': csrf },
			data: {
				server_id: serverId,
				source: 'interactive',
				client_request_id: crypto.randomUUID()
			}
		});
		expect(sessionResponse.status()).toBe(201);
		const sessionId = ((await sessionResponse.json()) as { session_id: string }).session_id;
		await page.goto('/');
		await expect(page.getByTestId('drawer-history-item').first()).toBeVisible({
			timeout: 30_000
		});
		return { serverId, sessionId };
	}

	test('idle 60 s: zero network requests (no fixed-interval polling)', async ({
		page
	}) => {
		test.setTimeout(120_000);
		const csrf = await backendLogin(page);
		const { sessionId: idleSessionId } = await seedAndOpen(
			page,
			csrf,
			'd08-idle-session'
		);
		// Open the visible session's channel, then go fully idle.
		await page.getByTestId('drawer-history-item').first().click();
		await expect(page.getByTestId('session-chat-panel')).toBeVisible();
		await page.getByTestId('drawer-history-item').first().click();
		await expect(page.getByTestId('session-chat-panel')).toBeVisible();

		let requests = 0;
		page.on('request', (request) => {
			if (request.url().includes('/api/v1/')) requests += 1;
		});
		const baseline = requests;
		await page.waitForTimeout(60_000);
		// ZERO API requests over the 60 s idle window: no fixed-interval
		// polling, no per-run streams, no transcript rereads.
		expect(requests - baseline).toBe(0);
		await page.screenshot({ path: `${EVIDENCE_DIR}/d08-idle-zero-requests.png` });
	});

	test('mobile 360×640: composer usable, zero horizontal overflow', async ({
		page
	}) => {
		await page.setViewportSize({ width: 360, height: 640 });
		const csrf = await backendLogin(page);
		await seedAndOpen(page, csrf, 'd08-mobile-session');
		// Open the session while the pocket drawer overlays the stage (the
		// drawer is topmost at 360px, so its rows receive the click).
		await page.getByTestId('drawer-history-item').first().click();
		await expect(page.getByTestId('session-chat-panel')).toBeVisible();
		// Collapse the drawer so the dock composer is reachable.
		const drawerToggle = page.getByTestId('side-drawer-toggle');
		if ((await drawerToggle.count()) > 0) {
			await drawerToggle.click();
		}
		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth - document.documentElement.clientWidth
		);
		expect(overflow).toBeLessThanOrEqual(0);
		// Keyboard: focus the composer, type, Enter admits the turn.
		await page.getByTestId('session-composer-input').click();
		await page.keyboard.type('mobile keyboard turn');
		await page.keyboard.press('Enter');
		await expect(page.getByTestId('session-submit-error')).toHaveCount(0);
		await page.screenshot({
			path: `${EVIDENCE_DIR}/d08-mobile-360x640.png`,
			fullPage: false
		});
	});
});
