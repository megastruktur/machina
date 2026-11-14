import { expect, test, type Page } from '@playwright/test';
import { build, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * nbi-fe-panel — real-browser proofs for the unified notification inbox
 * (`NotificationsPanel.svelte` + the feature-owned entry).
 *
 * Harness convention (reviewed `dashboard.spec.ts` fixture): the fixture
 * compiles the REAL feature panel, the REAL feature entry, the REAL panel
 * host/registry and the shared UI primitives into a throwaway fixture app
 * served from virtual modules — nothing hits disk and no real backend exists.
 * The fixture API mirrors the reviewed producers 1:1 (the global inbox
 * list/count/ack/unack/mark-all routes, the wakeup-only SSE stream binding
 * and `listProfiles`); recorded calls and pre-armed responses are the
 * test-observed boundary, not an imaginary backend.
 *
 * Covered acceptance (nbi_fe_panel.md):
 * 1. the registry accepts the entry (`scope_types: ['global']`, lazy loader)
 *    and the panel opens as a global window through the real PanelHost;
 * 2. the initial bounded page renders, Load more appends rows without
 *    duplicates, and `has_more` envelope honesty gates the control;
 * 3. read/unread toggling renders exactly the receipt; a failed stamp shows
 *    the envelope error WITHOUT an optimistic flip; the mark state persists
 *    across refetches;
 * 4. group headers show «— N loaded» page-bound counts only (R4-F1);
 *    empty/error/offline/config states are truthful;
 * 5. wakeup frames (and a clean stream close) trigger recorded refetches of
 *    the durable list + unread count; no content is ever parsed from frames.
 */

const FIXTURE_URL = '/mai-notifications-fixture/';
const FRONTEND_ROOT = new URL('../', import.meta.url).pathname;

const APP_ID = 'virtual:mai-notifications-app.svelte';
const ENTRY_ID = 'virtual:mai-notifications-entry.js';

const PANEL = `${FRONTEND_ROOT}src/lib/panels/NotificationsPanel.svelte`;
const ENTRY = `${FRONTEND_ROOT}src/lib/workspace/entries/14-web-notifications.ts`;
const PANELHOST = `${FRONTEND_ROOT}src/lib/workspace/PanelHost.svelte`;
const REGISTRY = `${FRONTEND_ROOT}src/lib/workspace/registry.ts`;
const HOST = `${FRONTEND_ROOT}tests/fixtures/component-host.svelte`;
const TOKENS = `${FRONTEND_ROOT}src/lib/ui/tokens.css`;
const CLIENT = `${FRONTEND_ROOT}src/lib/api/client.ts`;

/** Frozen identity fixtures (opaque UUIDs per contracts.md C01 rule 1). */
const SERVER_A = '8c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const SERVER_B = 'b7e4c2a0-3f6d-4e8b-9c1a-5d8f2e7b4c60';
const SERVER_A_NAME = 'alpha-core';
const SERVER_B_NAME = 'beta-core';
const WINDOW_GLOBAL = '0d0d0d0d-9999-4222-8333-444455556666';
const WINDOW_NOSEAM = '2f2f2f2f-9999-4222-8333-444455556666';

const ACK_STAMP = '2026-11-14T12:00:00Z';
const UNACK_STAMP = '2026-11-14T12:30:00Z';
/** Producer page bound mirrored by the panel's PAGE_LIMIT (AGENTS invariant). */
const PAGE_LIMIT = 50;
/** Total seeded notices: first page 50, the rest reachable via Load more. */
const TOTAL_NOTICES = 55;

/** Untruncated runtime-failure receipt: a page error is a harness defect. */
const pageErrors: string[] = [];

type Fail = { status: number; code: string; message: string };

interface StreamSession {
	open: boolean;
	aborted: boolean;
}

interface NotificationsWindowApi {
	calls: Array<{ op: string; payload?: unknown; args?: unknown[] }>;
	streams: StreamSession[];
	failNextInbox(failure: Fail | null): void;
	failNextUnread(failure: Fail | null): void;
	failNextAck(failure: Fail | null): void;
	failNextProfiles(failure: Fail | null): void;
	failNextStream(failure: Fail | null): void;
	armEmptyInbox(): void;
	deliverWakeup(frame: Record<string, unknown>): void;
	endStream(): void;
	registry: {
		kinds: string[];
		entryAccepted: string;
		scopeChecks: {
			globalScope: string;
			serverScope: string;
			unknownKind: string;
		};
		defaultGeometry: unknown;
		minGeometry: unknown;
		instanceKey: string | null;
	};
}

declare global {
	interface Window {
		__maiNotificationsApi?: NotificationsWindowApi;
		/** Set only if hostile fixture text ever executed — must stay undefined. */
		__maiXssFired?: boolean;
	}
}

type BuiltBundle = {
	output: Array<
		| { type: 'chunk'; isEntry: boolean; fileName: string; code: string }
		| { type: 'asset'; fileName: string; source: string | Uint8Array }
	>;
};

// ---------------------------------------------------------------------------
// The fixture app (compiled once per worker; mounted fresh per test)
// ---------------------------------------------------------------------------

const APP_SOURCE = `
<script>
	import PanelHost from '${PANELHOST}';
	import ComponentHost from '${HOST}';
	import { createPanelRegistry, definePanelEntry, singletonInstanceKey } from '${REGISTRY}';
	import { ApiError } from '${CLIENT}';
	import {
		NOTIFICATIONS_PANEL_ID,
		notificationsEntry,
		notificationsServices
	} from '${ENTRY}';

	const SERVER_A = '${SERVER_A}';
	const SERVER_B = '${SERVER_B}';
	const SERVER_A_NAME = '${SERVER_A_NAME}';
	const SERVER_B_NAME = '${SERVER_B_NAME}';
	const ACK_STAMP = '${ACK_STAMP}';
	const UNACK_STAMP = '${UNACK_STAMP}';
	const PAGE_LIMIT = ${PAGE_LIMIT};
	const TOTAL_NOTICES = ${TOTAL_NOTICES};

	// --- wire-view builders (the generated schema's exact field sets) --------

	function alertIdOf(index) {
		return 'aa000000-0000-4000-8000-' + String(index).padStart(12, '0');
	}

	function createdAtOf(index) {
		// Newest first: index 0 is the newest notice (route ORDER BY created_at DESC).
		return new Date(Date.UTC(2026, 9, 17, 9, 0, 0) - index * 60000).toISOString();
	}

	function ownerOf(index) {
		if (index % 11 === 4 || index % 11 === 10) return null;
		if (index % 11 === 7) return SERVER_B;
		return SERVER_A;
	}

	function noticeRow(index) {
		var external = index % 5 === 0;
		var severity = index % 3 === 0 ? 'info' : index % 3 === 1 ? 'warning' : 'critical';
		var acked = index === 2 || index === 5;
		return Object.assign(
			{
				alert_id: alertIdOf(index),
				server_id: ownerOf(index),
				origin: external ? 'external' : 'finding',
				dedup_key: 'notice-' + index,
				severity: severity,
				title: 'Notice ' + index + ' headline',
				summary: external ? null : 'Finding summary for notice ' + index,
				source: external ? 'host-monitor' : null,
				body: external
					? 'Full external message for notice ' + index +
						' — <img src=x onerror="window.__maiXssFired = true">'
					: null,
				context_ref: external ? null : 'finding:bb111111-1111-4333-8444-555566667777',
				delivery_status: 'delivered',
				created_at: createdAtOf(index),
				delivered_at: createdAtOf(index),
				acknowledged_at: acked ? ACK_STAMP : null,
				acked: acked,
				finding_id: external ? null : 'bb111111-1111-4333-8444-555566667777',
				run_id: null
			},
			{}
		);
	}

	function profileRow(serverId, name) {
		return {
			schema_version: 1,
			server_id: serverId,
			name: name,
			prompt_revision: 1,
			prompt_text: '',
			environment: 'linux_ssh',
			ssh: {
				host: name + '.example.internal',
				port: 22,
				user: 'deploy',
				host_key: 'a'.repeat(64),
				credential_ref: 'credential://' + name
			},
			privilege: 'none',
			capabilities: [],
			revision: 1,
			sha256: 'a'.repeat(64),
			projection: { revision: 1, stale: false }
		};
	}

	// --- the durable truth (fresh module evaluation per test) -----------------

	var notices = [];
	for (var index = 0; index < TOTAL_NOTICES; index += 1) notices.push(noticeRow(index));
	// Deterministic (name, id) order, as the profiles route answers.
	var profiles = [profileRow(SERVER_A, SERVER_A_NAME), profileRow(SERVER_B, SERVER_B_NAME)];

	function applyFilters(query, rows) {
		var filtered = rows;
		if (query && query.server_id) {
			filtered = filtered.filter(function (row) { return row.server_id === query.server_id; });
		} else if (query && query.scope === 'global') {
			filtered = filtered.filter(function (row) { return row.server_id === null; });
		}
		if (query && query.unread === true) {
			filtered = filtered.filter(function (row) { return !row.acked; });
		} else if (query && query.unread === false) {
			filtered = filtered.filter(function (row) { return row.acked; });
		}
		return filtered;
	}

	// --- recorded calls + armed behavior -------------------------------------

	var calls = [];
	/**
	 * One-shot failure arms, consumed by the next call to their op: the slot
	 * is cleared at the call site (a helper that nulled its own parameter
	 * would leave the arm set forever).
	 */
	var arms = { inbox: null, unread: null, ack: null, profiles: null, stream: null };
	var emptyInboxOnce = false;

	function record(op, payload, args) {
		calls.push({ op: op, payload: payload, args: args || [] });
	}

	function throwArm(name) {
		var failure = arms[name];
		if (failure) {
			arms[name] = null;
			throw new ApiError(failure.status, failure.code, failure.message);
		}
	}

	function deep(value) {
		return JSON.parse(JSON.stringify(value));
	}

	// Live wakeup stream sessions; the fake api closes over this list.
	var streams = [];

	function noticeById(alertId) {
		for (var i = 0; i < notices.length; i += 1) {
			if (notices[i].alert_id === alertId) return notices[i];
		}
		return null;
	}

	// --- fixture api: the reviewed producer surface, mirrored 1:1 ------------

	var api = {
		listInboxNotifications: async function (query) {
			record('listInboxNotifications', query ? deep(query) : null);
			throwArm('inbox');
			if (emptyInboxOnce) {
				emptyInboxOnce = false;
				return { alerts: [], limit: PAGE_LIMIT, offset: 0, total: 0, has_more: false };
			}
			var rows = applyFilters(query, notices);
			var offset = query && query.offset !== undefined ? query.offset : 0;
			var limit = query && query.limit !== undefined ? query.limit : PAGE_LIMIT;
			var pageRows = rows.slice(offset, offset + limit);
			return {
				alerts: deep(pageRows),
				limit: limit,
				offset: offset,
				total: rows.length,
				has_more: offset + pageRows.length < rows.length
			};
		},
		getUnreadNotificationCount: async function (query) {
			record('getUnreadNotificationCount', query ? deep(query) : null);
			throwArm('unread');
			return { total: applyFilters(query, notices).filter(function (row) { return !row.acked; }).length };
		},
		acknowledgeInboxNotification: async function (alertId) {
			record('acknowledgeInboxNotification', null, [alertId]);
			throwArm('ack');
			var row = noticeById(alertId);
			if (!row) throw new ApiError(404, 'not_found', 'no notice under this id');
			row.acked = true;
			row.acknowledged_at = ACK_STAMP;
			return { alert: deep(row) };
		},
		unacknowledgeInboxNotification: async function (alertId) {
			record('unacknowledgeInboxNotification', null, [alertId]);
			throwArm('ack');
			var row = noticeById(alertId);
			if (!row) throw new ApiError(404, 'not_found', 'no notice under this id');
			row.acked = false;
			row.acknowledged_at = UNACK_STAMP;
			return { alert: deep(row) };
		},
		markAllNotificationsRead: async function (query) {
			record('markAllNotificationsRead', query ? deep(query) : null);
			throwArm('ack');
			var updated = 0;
			for (var row of applyFilters(query, notices)) {
				if (!row.acked) {
					row.acked = true;
					row.acknowledged_at = ACK_STAMP;
					updated += 1;
				}
			}
			return { updated: updated };
		},
		// The client binding's exact contract: identity frames reach onWakeup,
		// a clean close fires onStreamEnd once and resolves, an abort only
		// resolves (never onStreamEnd), and an HTTP/network failure rejects.
		subscribeNotificationEvents: function (options) {
			throwArm('stream');
			var session = {
				open: true,
				aborted: false,
				onWakeup: options.onWakeup,
				onStreamEnd: options.onStreamEnd,
				resolve: null
			};
			streams.push(session);
			record('subscribeNotificationEvents', null);
			var pair = Promise.withResolvers();
			session.resolve = pair.resolve;
			if (options.signal && options.signal.aborted) {
				session.aborted = true;
				session.open = false;
				pair.resolve();
			} else if (options.signal) {
				options.signal.addEventListener(
					'abort',
					function () {
						session.aborted = true;
						session.open = false;
						pair.resolve();
					},
					{ once: true }
				);
			}
			return pair.promise.then(function () {
				session.open = false;
				var aborted = Boolean(options.signal && options.signal.aborted);
				if (!aborted && session.onStreamEnd) session.onStreamEnd();
			});
		},
		listProfiles: async function () {
			record('listProfiles', null);
			throwArm('profiles');
			return { servers: deep(profiles) };
		},
		listIngestKeys: async function (page) {
			record('listIngestKeys', page ? deep(page) : null);
			throwArm('inbox');
			return { keys: [], limit: 50, offset: 0, total: 0, has_more: false };
		},
		createIngestKey: async function (payload) {
			record('createIngestKey', payload ? deep(payload) : null);
			throwArm('ack');
			throw new ApiError(501, 'not_implemented', 'key minting is the sources task surface');
		},
		revokeIngestKey: async function (keyId) {
			record('revokeIngestKey', null, [keyId]);
			throwArm('ack');
			return { revoked: false };
		}
	};

	var services = notificationsServices(api);

	// --- registry: the REAL feature entry (scope validation + lazy loader) ----

	var registryResult = createPanelRegistry([notificationsEntry()]);
	if (!registryResult.ok) throw new Error('fixture registry rejected: ' + registryResult.issue.code);
	var registry = registryResult.value;
	var entryCheck = definePanelEntry(notificationsEntry());

	// --- hosts ----------------------------------------------------------------

	function descriptor(windowId, scope, z) {
		return {
			schema_version: 1,
			window_id: windowId,
			panel_kind: NOTIFICATIONS_PANEL_ID,
			scope: scope,
			geometry: { x: 8, y: 8, width: 760, height: 560 },
			presentation: 'normal',
			z: z,
			title_override: null
		};
	}

	function onPanelError(windowId, panelKind, error) {
		console.error(String(panelKind), String(windowId), error instanceof Error ? (error.stack ?? error.message) : error);
	}

	function checkCode(kind, scope) {
		var result = registry.checkScope(kind, scope);
		return result.ok ? 'ok' : result.issue.code;
	}

	var windowApi = {
		calls: calls,
		streams: streams,
		failNextInbox: function (failure) {
			arms.inbox = failure;
		},
		failNextUnread: function (failure) {
			arms.unread = failure;
		},
		failNextAck: function (failure) {
			arms.ack = failure;
		},
		failNextProfiles: function (failure) {
			arms.profiles = failure;
		},
		failNextStream: function (failure) {
			arms.stream = failure;
		},
		armEmptyInbox: function () {
			emptyInboxOnce = true;
		},
		// Identity-only delivery in spirit, deliberately HOSTILE in shape: the
		// raw frame object reaches onWakeup so the proof shows the panel reads
		// NOTHING from it (the real client additionally narrows the union).
		deliverWakeup: function (frame) {
			for (var i = streams.length - 1; i >= 0; i -= 1) {
				var session = streams[i];
				if (session.open && session.onWakeup) {
					session.onWakeup(frame);
					return;
				}
			}
			throw new Error('no open stream session to deliver to');
		},
		// The bounded server close: onStreamEnd fires on the resolving path.
		endStream: function () {
			for (var i = streams.length - 1; i >= 0; i -= 1) {
				var session = streams[i];
				if (session.open && session.resolve) {
					session.resolve();
					return;
				}
			}
			throw new Error('no open stream session to close');
		},
		registry: {
			kinds: Array.from(registry.knownPanelKinds()).sort(),
			entryAccepted: entryCheck.ok ? 'ok' : entryCheck.issue.code,
			scopeChecks: {
				globalScope: checkCode(NOTIFICATIONS_PANEL_ID, { kind: 'global' }),
				serverScope: checkCode(NOTIFICATIONS_PANEL_ID, {
					kind: 'server',
					server_id: SERVER_A
				}),
				unknownKind: checkCode('not-a-panel', { kind: 'global' })
			},
			defaultGeometry: registry.defaultGeometry(NOTIFICATIONS_PANEL_ID),
			minGeometry: registry.minGeometry(NOTIFICATIONS_PANEL_ID),
			instanceKey: singletonInstanceKey(NOTIFICATIONS_PANEL_ID, entryCheck.value, {
				kind: 'global'
			})
		}
	};

	window.__maiNotificationsApi = windowApi;
</script>

<section data-testid="host-global" aria-label="Host global notifications">
	<ComponentHost label="Host global notifications">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_GLOBAL}', { kind: 'global' }, 1)}
			{services}
		/>
	</ComponentHost>
</section>
<section data-testid="host-noseam" aria-label="Host without services">
	<ComponentHost label="Host without services">
		<PanelHost
			onpanelerror={onPanelError}
			{registry}
			descriptor={descriptor('${WINDOW_NOSEAM}', { kind: 'global' }, 2)}
		/>
	</ComponentHost>
</section>
`;

const ENTRY_SOURCE = `
import { mount } from 'svelte';
import App from '${APP_ID}';
import '${TOKENS}';
mount(App, { target: document.getElementById('app') });
`;

// ---------------------------------------------------------------------------
// Build the harness once per worker (same shape as the reviewed dashboard fixture)
// ---------------------------------------------------------------------------

/** Serves the virtual modules and the built lazy chunks; nothing hits disk. */
function harnessPlugin(sources: Record<string, string>): Plugin {
	return {
		name: 'mai-notifications-harness',
		resolveId(id) {
			// .svelte virtual modules must NOT get the conventional `\0` prefix:
			// vite-plugin-svelte excludes `\0` ids from Svelte compilation
			// (panel-host harness convention). The JS entry keeps the `\0`.
			if (id === APP_ID) return id;
			if (id === ENTRY_ID) return `\\0${id}`;
			return undefined;
		},
		load(id) {
			if (id === APP_ID) return sources[APP_ID];
			if (id === `\\0${ENTRY_ID}`) return sources[ENTRY_ID];
			return undefined;
		}
	};
}

/** Built lazy chunks (the entry's real dynamic panel import), served by name. */
let chunks: Array<{ fileName: string; code: string }> = [];
/** Chunk names actually fetched by the browser (lazy-loader proof). */
let servedChunks: string[] = [];
let harnessJs = '';
let harnessCss = '';

test.beforeAll(async () => {
	const sources: Record<string, string> = {
		[APP_ID]: APP_SOURCE,
		[ENTRY_ID]: ENTRY_SOURCE
	};
	const result = await build({
		configFile: false,
		root: FRONTEND_ROOT,
		logLevel: 'error',
		plugins: [svelte(), harnessPlugin(sources)],
		build: {
			write: false,
			emptyOutDir: true,
			cssCodeSplit: false,
			minify: false,
			outDir: 'node_modules/.mai-notifications-fixture',
			rollupOptions: {
				input: ENTRY_ID,
				output: {
					entryFileNames: 'demo.js',
					chunkFileNames: 'notifications-[hash].js',
					assetFileNames: 'demo[extname]'
				}
			}
		}
	});

	// `build()` is one-shot (no watcher configured); structural boundary type.
	const built = result as BuiltBundle | BuiltBundle[];
	const outputs = Array.isArray(built) ? built : [built];
	chunks = [];
	harnessJs = '';
	harnessCss = '';
	for (const { output } of outputs) {
		for (const item of output) {
			if (item.type === 'chunk') {
				if (item.isEntry) harnessJs += item.code;
				else chunks.push({ fileName: item.fileName, code: item.code });
			} else if (item.fileName.endsWith('.css')) {
				harnessCss += String(item.source);
			}
		}
	}
	if (!harnessJs || !harnessCss) {
		throw new Error('harness bundle incomplete: missing JS entry chunk or CSS asset');
	}
	if (chunks.length === 0) {
		throw new Error('harness bundle incomplete: the panel must build as a lazy chunk');
	}
});

test.beforeEach(async ({ page }) => {
	servedChunks = [];
	pageErrors.length = 0;
	const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MAI notifications fixture</title>
<link rel="stylesheet" href="${FIXTURE_URL}demo.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="${FIXTURE_URL}demo.js"><\/script>
</body>
</html>`;

	await page.route('**/*', (route) => {
		const { pathname } = new URL(route.request().url());
		if (!pathname.startsWith(FIXTURE_URL)) return route.abort();
		const rest = pathname.slice(FIXTURE_URL.length);
		if (rest === '' || rest.endsWith('/')) {
			return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
		}
		if (rest === 'demo.js') {
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: harnessJs });
		}
		if (rest === 'demo.css') {
			return route.fulfill({ contentType: 'text/css; charset=utf-8', body: harnessCss });
		}
		const chunk = chunks.find((candidate) => candidate.fileName === rest);
		if (chunk) {
			servedChunks.push(rest);
			return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: chunk.code });
		}
		return route.abort();
	});
	// Surface runtime failures in the receipt: an uncaught page error is a
	// harness/panel defect, never a silent pass.
	page.on('pageerror', (error) => {
		pageErrors.push(error.stack ?? error.message);
		console.log(`[pageerror] ${error.stack ?? error.message}`);
	});
	page.on('console', (message) => {
		if (message.type() === 'error') console.log(`[console.error] ${message.text()}`);
	});
	await page.goto(FIXTURE_URL);
});

// ---------------------------------------------------------------------------
// Fixture-API boundary (serialized dispatch; same convention as dashboard spec)
// ---------------------------------------------------------------------------

/** Call one fixture-api method inside the page; args must be serializable. */
async function apiCall(page: Page, method: string, args: unknown[] = []): Promise<void> {
	const pair: [string, unknown[]] = [method, args];
	await page.evaluate(([name, callArgs]) => {
		const value = window.__maiNotificationsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		const table = value as unknown as Record<string, (...a: never[]) => void>;
		const fn = table[name];
		if (typeof fn !== 'function') throw new Error(`unknown fixture method ${name}`);
		fn.apply(value, callArgs as never[]);
	}, pair);
}

interface RecordedCall {
	op: string;
	payload?: unknown;
	args?: unknown[];
}

/** Snapshot the fixture's recorded calls, streams and precomputed registry. */
async function apiState(page: Page): Promise<{
	calls: RecordedCall[];
	streams: Array<{ open: boolean; aborted: boolean }>;
	registry: NotificationsWindowApi['registry'];
}> {
	return page.evaluate(() => {
		const value = window.__maiNotificationsApi;
		if (!value) throw new Error('fixture api missing — app did not mount');
		return {
			calls: value.calls,
			streams: value.streams.map((session) => ({ open: session.open, aborted: session.aborted })),
			registry: value.registry
		};
	});
}

/** Test-only recorded-wire boundary: every recorded call for one op. */
function callsOf(calls: RecordedCall[], op: string): RecordedCall[] {
	return calls.filter((call) => call.op === op);
}

/** The alert id fixture for the row at newest-first position `index`. */
function alertIdOf(index: number): string {
	return `aa000000-0000-4000-8000-${String(index).padStart(12, '0')}`;
}

/** The global host's panel root, scoped for row/heading assertions. */
function panel(page: Page) {
	return page.getByTestId('host-global');
}

// ---------------------------------------------------------------------------
// 1. Registry acceptance + the global window opens through the real host
// ---------------------------------------------------------------------------

test('registry accepts the notifications entry and the panel opens as a global window', async ({
	page
}) => {
	const host = panel(page);

	// The REAL entry passes the REAL registry's validation.
	const { registry } = await apiState(page);
	expect(registry.entryAccepted).toBe('ok');
	expect(registry.kinds).toContain('notifications');
	expect(registry.scopeChecks.globalScope).toBe('ok');
	expect(registry.scopeChecks.serverScope).toBe('scope_kind_not_allowed');
	expect(registry.scopeChecks.unknownKind).toBe('unknown_panel_kind');
	expect(registry.instanceKey).toBe('notifications::global');
	expect(registry.defaultGeometry).toMatchObject({ width: 760, height: 560 });
	expect(registry.minGeometry).toMatchObject({ width: 420, height: 320 });

	// The panel opens as a GLOBAL window through the real PanelHost and its
	// lazy loader (the entry's dynamic import built as its own chunk).
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	expect(servedChunks.length).toBeGreaterThan(0);

	// The bounded initial page: producer limit 50 of the seeded 55.
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	await expect(host.getByTestId('unread-count')).toHaveText('Unread: 53');

	// Severity rides color AND the text label on every row.
	await expect(
		host.locator(`[data-alert-id="${alertIdOf(1)}"]`).getByTestId('row-severity-label')
	).toHaveText('warning');
	await expect(
		host.locator(`[data-alert-id="${alertIdOf(2)}"]`).getByTestId('row-severity-label')
	).toHaveText('critical');
	// External notices show their source chip; finding notices fall back to
	// the origin — nothing renders blank.
	await expect(
		host.locator(`[data-alert-id="${alertIdOf(0)}"]`).getByTestId('row-source')
	).toHaveText('host-monitor');
	await expect(
		host.locator(`[data-alert-id="${alertIdOf(1)}"]`).getByTestId('row-source')
	).toHaveText('finding');

	// Server display names resolve through listProfiles; a hostile body in
	// the fixture data never executes and renders as plain text only.
	expect(pageErrors).toEqual([]);
	expect(await page.evaluate(() => window.__maiXssFired)).toBeUndefined();
});

// ---------------------------------------------------------------------------
// 2. Bounded initial page, Load more dedup, has_more honesty
// ---------------------------------------------------------------------------

test('initial page renders 50 of 55 and Load more appends deduplicated rows once', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	// Page-bound grouping over the LOADED page only (R4-F1): exact «— N
	// loaded» headers; the unloaded five NEVER appear in any header.
	await expect(host.getByTestId('group-heading').filter({ hasText: 'alpha-core —' })).toHaveText(
		'alpha-core — 37 loaded'
	);
	await expect(host.getByTestId('group-heading').filter({ hasText: 'beta-core —' })).toHaveText(
		'beta-core — 4 loaded'
	);
	await expect(host.getByTestId('group-heading').filter({ hasText: 'Global —' })).toHaveText(
		'Global — 9 loaded'
	);
	// The oldest notice is beyond the first page and must not render yet.
	await expect(host.locator(`[data-alert-id="${alertIdOf(54)}"]`)).toHaveCount(0);

	const { calls } = await apiState(page);
	const initialLists = callsOf(calls, 'listInboxNotifications');
	expect(initialLists).toHaveLength(1);
	expect(initialLists[0].payload).toEqual({ limit: 50, offset: 0 });

	// Load more: exactly one more call at offset 50; rows append deduplicated.
	await expect(host.getByTestId('notifications-load-more')).toHaveText('Load more (50 of 55)');
	await host.getByTestId('notifications-load-more').click();
	await expect(host.getByTestId('notice-row')).toHaveCount(55);
	await expect(host.locator(`[data-alert-id="${alertIdOf(54)}"]`)).toHaveCount(1);
	await expect(host.getByTestId('group-heading').filter({ hasText: 'alpha-core —' })).toHaveText(
		'alpha-core — 40 loaded'
	);
	await expect(host.getByTestId('group-heading').filter({ hasText: 'beta-core —' })).toHaveText(
		'beta-core — 5 loaded'
	);
	await expect(host.getByTestId('group-heading').filter({ hasText: 'Global —' })).toHaveText(
		'Global — 10 loaded'
	);

	const uniqueIds = await page.evaluate(() => {
		const ids = Array.from(
			document.querySelectorAll('[data-testid="notice-row"]'),
			(row) => (row as HTMLElement).dataset.alertId
		);
		return { total: ids.length, unique: new Set(ids).size };
	});
	expect(uniqueIds.total).toBe(55);
	expect(uniqueIds.unique).toBe(55);

	const { calls: after } = await apiState(page);
	const lists = callsOf(after, 'listInboxNotifications');
	expect(lists).toHaveLength(2);
	expect(lists[1].payload).toEqual({ limit: 50, offset: 50 });

	// has_more envelope honesty: the final page removes the control and the
	// panel states the loaded count instead of implying more pages.
	await expect(host.getByTestId('notifications-load-more')).toHaveCount(0);
	await expect(host.getByTestId('notifications-page-count')).toHaveText(
		'55 of 55 notices loaded.'
	);
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 3. Receipt-driven read/unread toggling
// ---------------------------------------------------------------------------

test('read/unread toggling renders exactly the receipt and failures never flip optimistically', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	// Ack: the row flips ONLY to the receipt the route answered.
	const freshRow = host.locator(`[data-alert-id="${alertIdOf(0)}"]`);
	await expect(freshRow).toHaveAttribute('data-acked', 'false');
	await freshRow.getByTestId('row-toggle').click();
	await expect(freshRow).toHaveAttribute('data-acked', 'true');
	await expect(freshRow.getByTestId('row-readstate')).toHaveText('read');
	await expect(freshRow.getByTestId('row-toggle')).toHaveText('Mark unread');
	await expect(host.getByTestId('unread-count')).toHaveText('Unread: 52');

	let { calls } = await apiState(page);
	const acks = callsOf(calls, 'acknowledgeInboxNotification');
	expect(acks).toHaveLength(1);
	expect(acks[0].args).toEqual([alertIdOf(0)]);
	// The unread count rides the same canonical filters and was refetched.
	expect(callsOf(calls, 'getUnreadNotificationCount').length).toBeGreaterThanOrEqual(2);

	// Failed stamp: the envelope problem renders, the row NEVER flips.
	await apiCall(page, 'failNextAck', [
		{ status: 503, code: 'service_unavailable', message: 'stamp refused by the store' }
	]);
	const failingRow = host.locator(`[data-alert-id="${alertIdOf(1)}"]`);
	await expect(failingRow).toHaveAttribute('data-acked', 'false');
	await failingRow.getByTestId('row-toggle').click();
	await expect(host.getByTestId('notifications-problem')).toBeVisible();
	await expect(host.getByTestId('notifications-problem')).toHaveAttribute(
		'data-error-code',
		'service_unavailable'
	);
	await expect(host.getByTestId('notifications-problem')).toContainText('stamp refused');
	await expect(failingRow).toHaveAttribute('data-acked', 'false');
	await expect(failingRow.getByTestId('row-toggle')).toHaveText('Mark read');

	// Unack: the idempotent reverse stamp flips the row back per receipt.
	await freshRow.getByTestId('row-toggle').click();
	await expect(freshRow).toHaveAttribute('data-acked', 'false');
	await expect(freshRow.getByTestId('row-readstate')).toHaveText('unread');
	await expect(host.getByTestId('unread-count')).toHaveText('Unread: 53');
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'unacknowledgeInboxNotification')).toHaveLength(1);

	// Mark state persists across a wakeup-triggered refetch (durable truth).
	await freshRow.getByTestId('row-toggle').click();
	await expect(freshRow).toHaveAttribute('data-acked', 'true');
	await apiCall(page, 'deliverWakeup', [
		{ kind: 'updated', alert_id: alertIdOf(0), server_id: SERVER_A }
	]);
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	await expect(freshRow).toHaveAttribute('data-acked', 'true');
	const uniqueIds = await page.evaluate(() => {
		const ids = Array.from(
			document.querySelectorAll('[data-testid="notice-row"]'),
			(row) => (row as HTMLElement).dataset.alertId
		);
		return new Set(ids).size;
	});
	expect(uniqueIds).toBe(50);
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 4. Page-bound group headers + honest empty/error/offline/config states
// ---------------------------------------------------------------------------

test('group headers are page-bound only and every state is honest', async ({ page }) => {
	const host = panel(page);
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	// Every header reads «<group> — N loaded»; nothing implies per-server totals.
	const headings = host.getByTestId('group-heading');
	await expect(headings).toHaveCount(3);
	for (const heading of await headings.all()) {
		expect(await heading.textContent()).toMatch(/— \d+ loaded$/);
	}

	// A host opened WITHOUT the services bag fails closed visibly; the wired
	// host beside it is unaffected (no locator, no fallback).
	await expect(page.getByTestId('host-noseam').getByTestId('notifications-config-error')).toBeVisible();
	await expect(host.getByTestId('notifications-config-error')).toHaveCount(0);

	// Empty: the armed empty page renders the wiring hint, no Load more.
	await apiCall(page, 'armEmptyInbox');
	await host.getByTestId('notifications-refresh').click();
	await expect(host.getByTestId('notifications-empty')).toBeVisible();
	await expect(host.getByTestId('notifications-empty')).toContainText('ingest');
	await expect(host.getByTestId('notifications-load-more')).toHaveCount(0);

	// Error: the envelope code and message render with a Retry control.
	await apiCall(page, 'failNextInbox', [
		{ status: 503, code: 'service_unavailable', message: 'database unavailable' }
	]);
	await host.getByTestId('notifications-refresh').click();
	await expect(host.getByTestId('notifications-error')).toBeVisible();
	await expect(host.getByTestId('notifications-error')).toHaveAttribute(
		'data-error-code',
		'service_unavailable'
	);
	await expect(host.getByTestId('notifications-error')).toContainText('service_unavailable');
	// Retry on a disarmed producer restores the bounded page.
	await host.getByTestId('notifications-retry').click();
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	// Offline: the live loop's next subscribe fails once, the honest offline
	// note replaces «live», and the loop recovers into a fresh session.
	await apiCall(page, 'failNextStream', [
		{ status: 503, code: 'service_unavailable', message: 'stream unavailable' }
	]);
	await apiCall(page, 'endStream');
	await expect(host.getByTestId('live-status')).toContainText('live updates offline');
	await expect(host.locator('[data-testid="live-status"][data-live="offline"]')).toBeVisible();
	await expect
		.poll(
			async () => (await apiState(page)).streams.filter((session) => session.open).length,
			{ timeout: 5_000 }
		)
		.toBeGreaterThanOrEqual(1);
	await apiCall(page, 'deliverWakeup', [{ kind: 'resync' }]);
	await expect(host.getByTestId('live-status')).toContainText('live updates on');
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 5. Wakeup frames trigger recorded refetches; no content is parsed
// ---------------------------------------------------------------------------

test('wakeup frames and stream closes trigger refetches and their content is never parsed', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	let { calls } = await apiState(page);
	expect(callsOf(calls, 'listInboxNotifications')).toHaveLength(1);
	expect(callsOf(calls, 'getUnreadNotificationCount')).toHaveLength(1);

	// A created-frame with HOSTILE extra fields: the panel must refetch and
	// must not render, execute or otherwise read any frame field.
	await apiCall(page, 'deliverWakeup', [
		{
			kind: 'created',
			alert_id: 'ghost-1',
			server_id: null,
			title: '<img src=x onerror="window.__maiXssFired = true">',
			body: 'HOSTILE-WAKEUP-BODY'
		}
	]);
	await expect
		.poll(async () => callsOf((await apiState(page)).calls, 'listInboxNotifications').length)
		.toBe(2);
	await expect
		.poll(async () => callsOf((await apiState(page)).calls, 'getUnreadNotificationCount').length)
		.toBe(2);
	expect(await page.getByText('HOSTILE-WAKEUP-BODY').count()).toBe(0);
	expect(await page.evaluate(() => window.__maiXssFired)).toBeUndefined();

	// A resync frame refetches too.
	await apiCall(page, 'deliverWakeup', [{ kind: 'resync' }]);
	await expect
		.poll(async () => callsOf((await apiState(page)).calls, 'listInboxNotifications').length)
		.toBe(3);

	// Frames inside the debounce window coalesce into ONE durable refresh.
	await page.evaluate(() => {
		const value = window.__maiNotificationsApi;
		if (!value) throw new Error('fixture api missing');
		value.deliverWakeup({ kind: 'created', alert_id: 'ghost-2', server_id: null });
		value.deliverWakeup({ kind: 'updated', alert_id: 'ghost-2', server_id: null });
	});
	await expect
		.poll(async () => callsOf((await apiState(page)).calls, 'listInboxNotifications').length)
		.toBe(4);

	// The bounded stream self-close: onStreamEnd refetches AND the panel
	// resubscribes (a fresh open session), never a dead stream.
	await apiCall(page, 'endStream');
	await expect
		.poll(async () => callsOf((await apiState(page)).calls, 'listInboxNotifications').length)
		.toBe(5);
	await expect
		.poll(async () => {
			const state = await apiState(page);
			return state.streams.length;
		})
		.toBe(2);
	await expect
		.poll(async () => {
			const state = await apiState(page);
			return state.streams.filter((session) => session.open).length;
		})
		.toBe(1);

	// Every recorded inbox read stayed on the canonical bounded-page shape.
	const { calls: final } = await apiState(page);
	for (const call of callsOf(final, 'listInboxNotifications')) {
		expect(call.payload).toMatchObject({ limit: 50 });
		if (typeof call.payload === 'object' && call.payload !== null && 'offset' in call.payload) {
			const offset = call.payload.offset;
			expect(typeof offset).toBe('number');
			if (typeof offset === 'number') expect(offset % 50).toBe(0);
		}
	}
	for (const call of callsOf(final, 'getUnreadNotificationCount')) {
		expect(call.payload).toEqual({});
	}
	expect(pageErrors).toEqual([]);
});
