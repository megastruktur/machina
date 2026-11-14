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
 *
 * nbi-fe-filters adds (R4-F2 canonical filters):
 * 6. the server scope Select produces EXACTLY the canonical params — All
 *    (no server_id/scope/include_global), Global (scope=global, never
 *    server_id), enrolled server (server_id + include_global default true,
 *    honest false via the toggle);
 * 7. the read-state Select (unread true/false/omitted) and the origin chips
 *    (external/finding/job_result/omitted) compose into ONE canonical query;
 * 8. the debounced search commits ONCE server-side with the trimmed `q`,
 *    resets pagination to offset 0, composes with the other filters, and the
 *    shown/total counts stay honest;
 * 9. mark-all-read rides the EXACT active filter query, renders the
 *    receipt {updated} count, refetches the list + unread count durably and
 *    surfaces envelope errors without optimistic flips;
 * 10. switching filters mid-load is race-safe: a stale in-flight response
 *     from an older filter epoch is discarded, the newer filter state wins.
 *
 * nbi-fe-sources adds (the Sources tab; notification-inbox.md §6/§7):
 * 11. the tab seam defaults to Inbox (tests 1–10 stay unmodified) and
 *     Sources mounts the bounded ingest-key list: Load more appends rows
 *     deduplicated by `key_id`, honest loading/empty/error(+Retry) states,
 *     and NO secret column ever (no `key_hash`, no plaintext, anywhere);
 * 12. the mint flow shows the receipt plaintext EXACTLY ONCE in a modal —
 *     copy via the clipboard writes the exact secret with visible feedback,
 *     the «will not be shown again» warning renders, focus moves into the
 *     dialog on open and back to the create control on close, the §7 curl
 *     example carries the `$MAI_INGEST_KEY` placeholder (never the secret),
 *     and closing clears the plaintext from DOM and web storage entirely;
 * 13. the mint Scope select offers Global + enrolled servers (deterministic
 *     order); a Global mint omits `server_id`, a scoped mint carries it and
 *     the new row renders the enrolled display name;
 * 14. revoke is receipt-driven: an inline confirm gates the call, the row
 *     flips to the server's `revoked_at` only via the durable refetch after
 *     the receipt, and create/revoke envelope failures surface honestly
 *     without optimistic state changes.
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
/** The revoke receipt's server stamp (the fixture's durable truth). */
const REVOKE_STAMP = '2026-11-14T10:15:00.000Z';
/** Seeded ingest keys: keys page 20, the rest reachable via Load more. */
const TOTAL_KEYS = 26;

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
	failNextMarkAll(failure: Fail | null): void;
	failNextProfiles(failure: Fail | null): void;
	failNextStream(failure: Fail | null): void;
	failNextListKeys(failure: Fail | null): void;
	failNextCreateKey(failure: Fail | null): void;
	failNextRevokeKey(failure: Fail | null): void;
	armEmptyInbox(): void;
	armEmptyKeys(): void;
	/** Stall the NEXT inbox list response until releaseInboxHold resolves it. */
	holdNextInbox(): void;
	releaseInboxHold(): void;
	/** Stall the NEXT ingest-key list response until releaseKeysHold resolves it. */
	holdNextKeys(): void;
	releaseKeysHold(): void;
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
		/** Clipboard writes recorded by the addInitScript stub (Playwright-safe). */
		__maiClipboardWrites?: string[];
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
	const REVOKE_STAMP = '${REVOKE_STAMP}';
	const PAGE_LIMIT = ${PAGE_LIMIT};
	const TOTAL_NOTICES = ${TOTAL_NOTICES};
	const TOTAL_KEYS = ${TOTAL_KEYS};

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
		var jobResult = !external && index % 7 === 3;
		var severity = index % 3 === 0 ? 'info' : index % 3 === 1 ? 'warning' : 'critical';
		var acked = index === 2 || index === 5;
		return Object.assign(
			{
				alert_id: alertIdOf(index),
				server_id: ownerOf(index),
				origin: external ? 'external' : jobResult ? 'job_result' : 'finding',
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

	function ingestKeyIdOf(index) {
		return 'ee000000-0000-4000-8000-' + String(index).padStart(12, '0');
	}

	function ingestKeyRow(index) {
		// Newest first: index 0 is the newest key (route ORDER BY created_at DESC).
		var scoped = index % 3 === 1 ? SERVER_A : index % 3 === 2 ? SERVER_B : null;
		var revoked = index === 2;
		var expired = index === 3;
		return {
			key_id: ingestKeyIdOf(index),
			label: 'seed-key-' + index,
			server_id: scoped,
			created_at: new Date(Date.UTC(2026, 9, 10, 8, 0, 0) - index * 3600000).toISOString(),
			last_used_at:
				index % 2 === 0
					? new Date(Date.UTC(2026, 9, 10, 8, 0, 0) - index * 1800000).toISOString()
					: null,
			expires_at: expired ? new Date(Date.UTC(2026, 9, 10, 7, 59, 59)).toISOString() : null,
			revoked_at: revoked ? new Date(Date.UTC(2026, 9, 12, 7, 30, 0)).toISOString() : null
		};
	}

	// --- the durable truth (fresh module evaluation per test) -----------------

	var notices = [];
	for (var index = 0; index < TOTAL_NOTICES; index += 1) notices.push(noticeRow(index));
	// Deterministic (name, id) order, as the profiles route answers.
	var profiles = [profileRow(SERVER_A, SERVER_A_NAME), profileRow(SERVER_B, SERVER_B_NAME)];
	var ingestKeys = [];
	for (var keyIndex = 0; keyIndex < TOTAL_KEYS; keyIndex += 1) ingestKeys.push(ingestKeyRow(keyIndex));

	function ingestKeyById(keyId) {
		for (var i = 0; i < ingestKeys.length; i += 1) {
			if (ingestKeys[i].key_id === keyId) return ingestKeys[i];
		}
		return null;
	}

	// Mint-time truth: every minted key carries the same receipt stamp and
	// unshifts (newest first), so its key_id stays deterministic per test.
	var CREATE_STAMP = '2026-11-14T11:00:00.000Z';
	var mintedKeys = 0;

	function applyFilters(query, rows) {
		var filtered = rows;
		if (!query) return filtered;
		// R4-F2: server_id selects that server; global (NULL-server) rows ride
		// along unless include_global is explicitly false (route default true).
		// scope=global is the mutually exclusive alternative: NULL-server only.
		if (query.server_id) {
			var includeGlobal = query.include_global !== false;
			filtered = filtered.filter(function (row) {
				return row.server_id === query.server_id || (includeGlobal && row.server_id === null);
			});
		} else if (query.scope === 'global') {
			filtered = filtered.filter(function (row) { return row.server_id === null; });
		}
		if (query.unread === true) {
			filtered = filtered.filter(function (row) { return !row.acked; });
		} else if (query.unread === false) {
			filtered = filtered.filter(function (row) { return row.acked; });
		}
		if (query.origin) {
			filtered = filtered.filter(function (row) { return row.origin === query.origin; });
		}
		if (typeof query.q === 'string' && query.q !== '') {
			// Canonical ONE field set (R2-F1): ILIKE over title/summary/body/source.
			var needle = query.q.toLowerCase();
			filtered = filtered.filter(function (row) {
				return ['title', 'summary', 'body', 'source'].some(function (field) {
					var value = row[field];
					return typeof value === 'string' && value.toLowerCase().indexOf(needle) !== -1;
				});
			});
		}
		return filtered;
	}

	// --- recorded calls + armed behavior -------------------------------------

	var calls = [];
	/**
	 * One-shot failure arms, consumed by the next call to their op: the slot
	 * is cleared at the call site (a helper that nulled its own parameter
	 * would leave the arm set forever). The ingest-key ops carry DEDICATED
	 * arms (nbi-fe-sources) — an inbox/profile failure never fires on a key
	 * call and vice versa.
	 */
	var arms = {
		inbox: null,
		unread: null,
		ack: null,
		markAll: null,
		profiles: null,
		stream: null,
		listKeys: null,
		createKey: null,
		revokeKey: null
	};
	var emptyInboxOnce = false;
	var emptyKeysOnce = false;
	/**
	 * Inbox response gates: 'holdNextInbox' arms one gate, the next
	 * listInboxNotifications call consumes and awaits it, and
	 * 'releaseInboxHold' resolves it — the race-safety proof's slow lane.
	 * The ingest-key list mirrors the same machinery ('holdNextKeys').
	 */
	var inboxGates = [];
	var heldInbox = [];
	var keysGates = [];
	var heldKeys = [];

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
			if (inboxGates.length > 0) {
				var gate = inboxGates.shift();
				heldInbox.push(gate);
				await gate.promise;
			}
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
			throwArm('markAll');
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
		// The ingest-key surface (nbi-fe-sources): producer-mirroring page
		// envelope, mint receipts carrying the plaintext EXACTLY ONCE, and an
		// idempotent revoke receipt. listIngestKeys mirrors the inbox page
		// builder: explicit limit/offset ride the query, total/has_more come
		// from the pre-slice collection.
		listIngestKeys: async function (page) {
			record('listIngestKeys', page ? deep(page) : null);
			throwArm('listKeys');
			if (keysGates.length > 0) {
				var keysGate = keysGates.shift();
				heldKeys.push(keysGate);
				await keysGate.promise;
			}
			if (emptyKeysOnce) {
				emptyKeysOnce = false;
				return { keys: [], limit: 50, offset: 0, total: 0, has_more: false };
			}
			var keyOffset = page && page.offset !== undefined ? page.offset : 0;
			var keyLimit = page && page.limit !== undefined ? page.limit : 50;
			var keyRows = ingestKeys.slice(keyOffset, keyOffset + keyLimit);
			return {
				keys: deep(keyRows),
				limit: keyLimit,
				offset: keyOffset,
				total: ingestKeys.length,
				has_more: keyOffset + keyRows.length < ingestKeys.length
			};
		},
		createIngestKey: async function (payload) {
			record('createIngestKey', payload ? deep(payload) : null);
			throwArm('createKey');
			// §6: label 1..64, omitting server_id mints a GLOBAL key, the
			// plaintext rides the receipt EXACTLY ONCE, only metadata persists.
			var label = payload && typeof payload.label === 'string' ? payload.label : '';
			if (label.trim().length < 1 || label.trim().length > 64) {
				throw new ApiError(422, 'validation_failed', 'label must be 1..64 characters');
			}
			mintedKeys += 1;
			var minted = {
				key_id: 'ee111111-0000-4000-8000-' + String(mintedKeys).padStart(12, '0'),
				label: label.trim(),
				server_id: payload && payload.server_id ? payload.server_id : null,
				created_at: CREATE_STAMP,
				last_used_at: null,
				expires_at: payload && payload.expires_at ? payload.expires_at : null,
				revoked_at: null
			};
			// Newest first — the route ORDER BY created_at DESC.
			ingestKeys.unshift(minted);
			var receipt = deep(minted);
			// The one-time secret: deterministic per mint, never stored on the row.
			receipt.key = 'mai-k-' + minted.key_id;
			return receipt;
		},
		revokeIngestKey: async function (keyId) {
			record('revokeIngestKey', null, [keyId]);
			throwArm('revokeKey');
			var keyRow = ingestKeyById(keyId);
			// Idempotent receipt: revoked=false covers unknown/already-revoked.
			if (!keyRow || keyRow.revoked_at !== null) return { key_id: keyId, revoked: false };
			keyRow.revoked_at = REVOKE_STAMP;
			return { key_id: keyId, revoked: true };
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
		failNextMarkAll: function (failure) {
			arms.markAll = failure;
		},
		failNextProfiles: function (failure) {
			arms.profiles = failure;
		},
		failNextStream: function (failure) {
			arms.stream = failure;
		},
		failNextListKeys: function (failure) {
			arms.listKeys = failure;
		},
		failNextCreateKey: function (failure) {
			arms.createKey = failure;
		},
		failNextRevokeKey: function (failure) {
			arms.revokeKey = failure;
		},
		armEmptyInbox: function () {
			emptyInboxOnce = true;
		},
		armEmptyKeys: function () {
			emptyKeysOnce = true;
		},
		holdNextInbox: function () {
			inboxGates.push(Promise.withResolvers());
		},
		releaseInboxHold: function () {
			var gate = heldInbox.shift();
			if (!gate) throw new Error('no inbox call is currently held');
			gate.resolve();
		},
		holdNextKeys: function () {
			keysGates.push(Promise.withResolvers());
		},
		releaseKeysHold: function () {
			var keyGate = heldKeys.shift();
			if (!keyGate) throw new Error('no ingest-key call is currently held');
			keyGate.resolve();
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
	// Deterministic clipboard seam: headless writeText permission behavior is
	// environment-dependent, so the stub records every write for assertion.
	await page.addInitScript(() => {
		const writes: string[] = [];
		Object.defineProperty(navigator, 'clipboard', {
			value: {
				writeText: (text: string) => {
					writes.push(text);
					return Promise.resolve();
				}
			},
			configurable: true
		});
		window.__maiClipboardWrites = writes;
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

/** The ingest-key fixture id for the seed at newest-first position `index`. */
function keyIdOf(index: number): string {
	return `ee000000-0000-4000-8000-${String(index).padStart(12, '0')}`;
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

// ---------------------------------------------------------------------------
// 6. Server scope select — exactly the canonical params (R4-F2)
// ---------------------------------------------------------------------------

test('server scope select sends exactly the canonical params for All, Global and enrolled servers', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	// The scope Select offers All / Global / enrolled, deterministic order.
	const scopeSelect = host.getByTestId('filter-scope');
	expect(await scopeSelect.locator('option').allTextContents()).toEqual([
		'All servers',
		'Global only',
		SERVER_A_NAME,
		SERVER_B_NAME
	]);

	// ALL: neither server_id nor scope, no include_global — the bare page.
	let { calls } = await apiState(page);
	expect(callsOf(calls, 'listInboxNotifications')[0].payload).toEqual({ limit: 50, offset: 0 });
	expect(callsOf(calls, 'getUnreadNotificationCount')[0].payload).toEqual({});

	// GLOBAL: scope=global and NEVER server_id/include_global. All ten
	// global notices (across the seeded 55) fit one page — has_more honest.
	await scopeSelect.selectOption('global');
	await expect(host.getByTestId('notice-row')).toHaveCount(10);
	await expect(host.getByTestId('group-heading')).toHaveCount(1);
	await expect(host.getByTestId('group-heading')).toHaveText('Global — 10 loaded');
	await expect(host.getByTestId('notifications-page-count')).toHaveText(
		'10 of 10 notices loaded.'
	);
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[1].payload).toEqual({
		scope: 'global',
		limit: 50,
		offset: 0
	});
	expect(callsOf(calls, 'getUnreadNotificationCount')[1].payload).toEqual({ scope: 'global' });

	// ENROLLED server: server_id + include_global default true (all ten
	// global notices ride along: 40 alpha + 10 global = one full page).
	await scopeSelect.selectOption(SERVER_A);
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	await expect(host.getByTestId('group-heading')).toHaveCount(2);
	await expect(host.getByTestId('group-heading').filter({ hasText: 'alpha-core —' })).toHaveText(
		'alpha-core — 40 loaded'
	);
	await expect(host.getByTestId('group-heading').filter({ hasText: 'Global —' })).toHaveText(
		'Global — 10 loaded'
	);
	await expect(
		host.locator(`[data-testid="notice-group"][data-server="${SERVER_B}"]`)
	).toHaveCount(0);
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[2].payload).toEqual({
		server_id: SERVER_A,
		include_global: true,
		limit: 50,
		offset: 0
	});
	expect(callsOf(calls, 'getUnreadNotificationCount')[2].payload).toEqual({
		server_id: SERVER_A,
		include_global: true
	});

	// include_global=false: strictly that server — the Global group disappears.
	await expect(host.getByTestId('filter-include-global')).toBeVisible();
	await host.getByTestId('filter-include-global').uncheck();
	await expect(host.getByTestId('notice-row')).toHaveCount(40);
	await expect(host.getByTestId('group-heading')).toHaveCount(1);
	await expect(host.getByTestId('group-heading')).toHaveText('alpha-core — 40 loaded');
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[3].payload).toEqual({
		server_id: SERVER_A,
		include_global: false,
		limit: 50,
		offset: 0
	});

	// Back to ALL: the bare canonical page again (bounded, more pages exist).
	await scopeSelect.selectOption('all');
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	await expect(host.getByTestId('notifications-load-more')).toHaveText('Load more (50 of 55)');
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[4].payload).toEqual({ limit: 50, offset: 0 });
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 7. Read-state select + origin chips — canonical params, composed
// ---------------------------------------------------------------------------

test('read-state select and origin chips send exactly the canonical params and compose', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	const readSelect = host.getByTestId('filter-read');
	const chip = (value: string) => host.getByTestId(`filter-origin-${value}`);

	// Unread: unread=true; every rendered row is unread.
	await readSelect.selectOption('unread');
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	await expect(host.locator('[data-testid="notice-row"][data-acked="true"]')).toHaveCount(0);
	let { calls } = await apiState(page);
	expect(callsOf(calls, 'listInboxNotifications')[1].payload).toEqual({
		unread: true,
		limit: 50,
		offset: 0
	});
	expect(callsOf(calls, 'getUnreadNotificationCount')[1].payload).toEqual({ unread: true });

	// Read: unread=false; exactly the two seeded read notices. The unread
	// COUNT reads under the SAME filter surface (0 unread among read rows).
	await readSelect.selectOption('read');
	await expect(host.getByTestId('notice-row')).toHaveCount(2);
	await expect(host.locator('[data-testid="notice-row"][data-acked="false"]')).toHaveCount(0);
	await expect(host.getByTestId('unread-count')).toHaveText('Unread: 0');
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[2].payload).toEqual({
		unread: false,
		limit: 50,
		offset: 0
	});

	// Back to All: the unread param leaves the wire entirely.
	await readSelect.selectOption('all');
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[3].payload).toEqual({ limit: 50, offset: 0 });

	// Origin chips: external → finding → job_result → all. Filtered totals
	// span the whole seed (11 external / 38 finding / 6 job_result) and each
	// fits one honest page.
	await chip('external').click();
	await expect(host.getByTestId('notice-row')).toHaveCount(11);
	await expect(host.getByTestId('notifications-page-count')).toHaveText(
		'11 of 11 notices loaded.'
	);
	await expect(host.getByTestId('row-source')).toHaveText(
		Array.from({ length: 11 }, () => 'host-monitor')
	);
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[4].payload).toEqual({
		origin: 'external',
		limit: 50,
		offset: 0
	});

	await chip('finding').click();
	await expect(host.getByTestId('notice-row')).toHaveCount(38);
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[5].payload).toEqual({
		origin: 'finding',
		limit: 50,
		offset: 0
	});

	await chip('job_result').click();
	await expect(host.getByTestId('notice-row')).toHaveCount(6);
	await expect(host.getByTestId('notifications-page-count')).toHaveText(
		'6 of 6 notices loaded.'
	);
	await expect(host.getByTestId('row-source')).toHaveText(
		Array.from({ length: 6 }, () => 'job_result')
	);
	await expect(chip('job_result')).toHaveAttribute('aria-pressed', 'true');
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[6].payload).toEqual({
		origin: 'job_result',
		limit: 50,
		offset: 0
	});

	await chip('all').click();
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	await expect(host.getByTestId('notifications-load-more')).toHaveText('Load more (50 of 55)');
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'listInboxNotifications')[7].payload).toEqual({ limit: 50, offset: 0 });

	// Combined composition: Unread + Finding rides ONE canonical query
	// (38 finding notices, the one acked finding excluded server-side).
	await chip('finding').click();
	await readSelect.selectOption('unread');
	await expect(host.getByTestId('notice-row')).toHaveCount(37);
	({ calls } = await apiState(page));
	const combined = callsOf(calls, 'listInboxNotifications');
	expect(combined[combined.length - 1].payload).toEqual({
		unread: true,
		origin: 'finding',
		limit: 50,
		offset: 0
	});
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 8. Debounced server-side search: offset reset, composition, honest counts
// ---------------------------------------------------------------------------

test('debounced search commits once server-side, resets pagination and composes with filters', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	// Whitespace-only input NEVER commits: no server call at all.
	const search = host.getByTestId('notifications-search');
	const callsBeforeWhitespace = (await apiState(page)).calls.length;
	await search.fill('   ');
	await page.waitForTimeout(500);
	expect((await apiState(page)).calls.length).toBe(callsBeforeWhitespace);

	// Grow the loaded page first: the next committed search must reset it.
	await host.getByTestId('notifications-load-more').click();
	await expect(host.getByTestId('notice-row')).toHaveCount(55);

	// Two rapid edits coalesce into exactly ONE server commit with the
	// settled text; pagination resets to offset 0 (was 55 loaded rows).
	await search.fill('hea');
	await search.fill('headline');
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	await expect(host.getByTestId('notifications-load-more')).toHaveText('Load more (50 of 55)');
	let { calls } = await apiState(page);
	let searchLists = callsOf(calls, 'listInboxNotifications').slice(2);
	expect(searchLists).toHaveLength(1);
	expect(searchLists[0].payload).toEqual({ q: 'headline', limit: 50, offset: 0 });
	expect(callsOf(calls, 'getUnreadNotificationCount')[1].payload).toEqual({ q: 'headline' });

	// Honest counts on the searched page: shown / total from the envelopes.
	await expect(host.getByTestId('notifications-search-count')).toHaveText('50 / 55');

	// Load more under the active search rides the same q at offset 50.
	await host.getByTestId('notifications-load-more').click();
	await expect(host.getByTestId('notice-row')).toHaveCount(55);
	await expect(host.getByTestId('notifications-search-count')).toHaveText('55 / 55');
	({ calls } = await apiState(page));
	searchLists = callsOf(calls, 'listInboxNotifications').slice(2);
	expect(searchLists).toHaveLength(2);
	expect(searchLists[1].payload).toEqual({ q: 'headline', limit: 50, offset: 50 });

	// Composition: the Read filter under the live search — ONE canonical
	// query; only the two seeded read notices match both filters.
	await host.getByTestId('filter-read').selectOption('read');
	await expect(host.getByTestId('notice-row')).toHaveCount(2);
	await expect(host.getByTestId('notifications-search-count')).toHaveText('2 / 2');
	({ calls } = await apiState(page));
	searchLists = callsOf(calls, 'listInboxNotifications').slice(2);
	expect(searchLists).toHaveLength(3);
	expect(searchLists[2].payload).toEqual({ unread: false, q: 'headline', limit: 50, offset: 0 });

	// Clearing the search via the primitive drops `q` from the wire entirely
	// (the debounced commit rides after its 250 ms window — poll for it).
	await host.getByTestId('notifications-search-clear').click();
	await expect
		.poll(async () => callsOf((await apiState(page)).calls, 'listInboxNotifications').length)
		.toBe(6); // 2 pre-search page loads + 4 search-flow commits
	await expect(host.getByTestId('notice-row')).toHaveCount(2);
	await expect(host.getByTestId('notifications-search-count')).toHaveText('2 / 2');
	({ calls } = await apiState(page));
	searchLists = callsOf(calls, 'listInboxNotifications').slice(2);
	expect(searchLists).toHaveLength(4);
	expect(searchLists[3].payload).toEqual({ unread: false, limit: 50, offset: 0 });
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 9. Mark-all-read: current filters ride, receipt renders, durable refetch
// ---------------------------------------------------------------------------

test('mark-all-read sends the active filters, renders the receipt and refetches durably', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	// Unread filter first: mark-all must ride EXACTLY {unread: true}.
	await host.getByTestId('filter-read').selectOption('unread');
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	let { calls } = await apiState(page);
	const listsBefore = callsOf(calls, 'listInboxNotifications').length;
	const countsBefore = callsOf(calls, 'getUnreadNotificationCount').length;

	await host.getByTestId('notifications-mark-all-read').click();
	await expect(host.getByTestId('mark-all-receipt')).toHaveText(
		'Marked 53 read under the current filters.'
	);
	// Never optimistic: the durable refetch replaced the page — the SAME
	// unread filter now yields an honest empty state and an honest count.
	await expect(host.getByTestId('notifications-empty')).toBeVisible();
	await expect(host.getByTestId('unread-count')).toHaveText('Unread: 0');
	({ calls } = await apiState(page));
	const marks = callsOf(calls, 'markAllNotificationsRead');
	expect(marks).toHaveLength(1);
	expect(marks[0].payload).toEqual({ unread: true });
	expect(callsOf(calls, 'listInboxNotifications')).toHaveLength(listsBefore + 1);
	expect(callsOf(calls, 'listInboxNotifications')[listsBefore].payload).toEqual({
		unread: true,
		limit: 50,
		offset: 0
	});
	expect(callsOf(calls, 'getUnreadNotificationCount')).toHaveLength(countsBefore + 1);
	expect(callsOf(calls, 'getUnreadNotificationCount')[countsBefore].payload).toEqual({
		unread: true
	});

	// A filter change clears the stale receipt; a scoped mark-all rides the
	// COMPOSED current query — the read state is still «Unread», so the
	// canonical wire is scope+unread together (0 unread globals — everything
	// is read already).
	await host.getByTestId('filter-scope').selectOption('global');
	await expect(host.getByTestId('mark-all-receipt')).toHaveCount(0);
	await expect(host.getByTestId('notifications-empty')).toBeVisible();
	await host.getByTestId('notifications-mark-all-read').click();
	await expect(host.getByTestId('mark-all-receipt')).toHaveText(
		'Marked 0 read under the current filters.'
	);
	({ calls } = await apiState(page));
	const marksAll = callsOf(calls, 'markAllNotificationsRead');
	expect(marksAll).toHaveLength(2);
	expect(marksAll[1].payload).toEqual({ scope: 'global', unread: true });

	// Envelope failure: the problem banner surfaces, the receipt stays the
	// last honest one, and the empty state is untouched.
	await apiCall(page, 'failNextMarkAll', [
		{ status: 503, code: 'service_unavailable', message: 'mark-all refused by the store' }
	]);
	await host.getByTestId('notifications-mark-all-read').click();
	await expect(host.getByTestId('notifications-problem')).toBeVisible();
	await expect(host.getByTestId('notifications-problem')).toHaveAttribute(
		'data-error-code',
		'service_unavailable'
	);
	await expect(host.getByTestId('notifications-problem')).toContainText('mark-all refused');
	await expect(host.getByTestId('mark-all-receipt')).toHaveText(
		'Marked 0 read under the current filters.'
	);
	({ calls } = await apiState(page));
	expect(callsOf(calls, 'markAllNotificationsRead')).toHaveLength(3);
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 10. Race-safe filter switching: the newer filter epoch wins
// ---------------------------------------------------------------------------

test('switching filters mid-load ignores the stale in-flight response', async ({ page }) => {
	const host = panel(page);
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	// Hold the NEXT inbox response: the Global filter load stalls in flight
	// and must NOT render over the still-visible All page.
	await apiCall(page, 'holdNextInbox');
	await host.getByTestId('filter-scope').selectOption('global');
	await expect(host.getByTestId('notice-row')).toHaveCount(50);

	// Switch filters WHILE the Global load is still in flight: the server
	// load is fast and becomes the live page (50 rows: 40 alpha + 10 global,
	// two of them carrying the seeded read stamps).
	await host.getByTestId('filter-scope').selectOption(SERVER_A);
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	await expect(host.getByTestId('unread-count')).toHaveText('Unread: 48');

	let { calls } = await apiState(page);
	const lists = callsOf(calls, 'listInboxNotifications');
	expect(lists).toHaveLength(3);
	expect(lists[1].payload).toEqual({ scope: 'global', limit: 50, offset: 0 });
	expect(lists[2].payload).toEqual({
		server_id: SERVER_A,
		include_global: true,
		limit: 50,
		offset: 0
	});

	// Release the stale Global-only page: it must be DISCARDED (older epoch,
	// older filter snapshot), never rendered over the newer server page.
	await apiCall(page, 'releaseInboxHold');
	await expect
		.poll(async () => callsOf((await apiState(page)).calls, 'listInboxNotifications').length)
		.toBe(3); // the stale completion raced in no fresh load
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	await expect(host.locator('[data-testid="notice-row"][data-acked="true"]')).toHaveCount(2);
	await expect(host.getByTestId('group-heading')).toHaveCount(2);
	await expect(host.getByTestId('group-heading').filter({ hasText: 'alpha-core —' })).toHaveText(
		'alpha-core — 40 loaded'
	);
	await expect(host.getByTestId('group-heading').filter({ hasText: 'Global —' })).toHaveText(
		'Global — 10 loaded'
	);
	await expect(host.getByTestId('unread-count')).toHaveText('Unread: 48');
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 11. Sources tab: bounded key list, Load more dedup, honest states, NO secrets
// ---------------------------------------------------------------------------

test('sources tab: bounded key list loads pages honestly and never shows key material', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	// Inbox is the DEFAULT tab: the inbox toolbar is live before any switch.
	await expect(host.getByTestId('notifications-search')).toBeVisible();
	await expect(host.getByTestId('keys-root')).toHaveCount(0);

	// Switching to Sources mounts the ingest-key section; the inbox toolbar
	// unmounts (the surfaces are mutually exclusive tab content).
	await host.getByTestId('tab-sources').click();
	await expect(host.getByTestId('keys-root')).toBeVisible();
	await expect(host.getByTestId('key-row')).toHaveCount(20);
	await expect(host.getByTestId('notifications-search')).toHaveCount(0);

	// Metadata columns only — no `key_hash`, no plaintext material, anywhere.
	const bodyText = await page.evaluate(() => document.body.textContent ?? '');
	expect(bodyText).not.toContain('key_hash');
	expect(bodyText).not.toContain('mai-k-');

	// Scope renders Global / enrolled display names in seeded variety; the
	// revoked seed shows its status and the server's stamp, the active seed
	// reads active with an honest em-dash.
	await expect(host.getByTestId('key-row').nth(0).getByTestId('key-scope')).toHaveText('Global');
	await expect(host.getByTestId('key-row').nth(1).getByTestId('key-scope')).toHaveText(
		SERVER_A_NAME
	);
	await expect(host.getByTestId('key-row').nth(2).getByTestId('key-scope')).toHaveText(
		SERVER_B_NAME
	);
	await expect(host.getByTestId('key-row').nth(0).getByTestId('key-status')).toHaveText('active');
	await expect(host.getByTestId('key-row').nth(0).getByTestId('key-revoked-at')).toHaveText('—');
	await expect(host.getByTestId('key-row').nth(2).getByTestId('key-status')).toHaveText('revoked');
	await expect(host.getByTestId('key-row').nth(2)).toHaveAttribute('data-revoked', 'true');

	const { calls } = await apiState(page);
	const keyLists = callsOf(calls, 'listIngestKeys');
	expect(keyLists).toHaveLength(1);
	expect(keyLists[0].payload).toEqual({ limit: 20, offset: 0 });

	// Load more: exactly one more call at offset 20; rows append deduplicated.
	await expect(host.getByTestId('keys-load-more')).toHaveText('Load more (20 of 26)');
	await host.getByTestId('keys-load-more').click();
	await expect(host.getByTestId('key-row')).toHaveCount(26);
	await expect(host.getByTestId('key-row').nth(25).getByTestId('key-label')).toHaveText(
		'seed-key-25'
	);
	const uniqueKeyIds = await page.evaluate(() => {
		const ids = Array.from(
			document.querySelectorAll('[data-testid="key-row"]'),
			(row) => (row as HTMLElement).dataset.keyId
		);
		return { total: ids.length, unique: new Set(ids).size };
	});
	expect(uniqueKeyIds.total).toBe(26);
	expect(uniqueKeyIds.unique).toBe(26);
	await expect(host.getByTestId('keys-load-more')).toHaveCount(0);
	await expect(host.getByTestId('keys-page-count')).toHaveText('26 of 26 ingest keys loaded.');
	const { calls: afterLoadMore } = await apiState(page);
	const keyListsAfter = callsOf(afterLoadMore, 'listIngestKeys');
	expect(keyListsAfter[1].payload).toEqual({ limit: 20, offset: 20 });

	// Error state: the envelope code renders with a Retry control; retry on a
	// disarmed producer restores the bounded page.
	await apiCall(page, 'failNextListKeys', [
		{ status: 503, code: 'service_unavailable', message: 'keys store unavailable' }
	]);
	await host.getByTestId('keys-refresh').click();
	await expect(host.getByTestId('keys-error')).toBeVisible();
	await expect(host.getByTestId('keys-error')).toHaveAttribute(
		'data-error-code',
		'service_unavailable'
	);
	await expect(host.getByTestId('keys-error')).toContainText('keys store unavailable');
	await host.getByTestId('keys-retry').click();
	await expect(host.getByTestId('key-row')).toHaveCount(20);

	// Empty state: the armed empty page renders the minting hint.
	await apiCall(page, 'armEmptyKeys');
	await host.getByTestId('keys-refresh').click();
	await expect(host.getByTestId('keys-empty')).toBeVisible();

	// Loading state: the held page keeps an honest loading note until release.
	await apiCall(page, 'holdNextKeys');
	await host.getByTestId('keys-refresh').click();
	await expect(host.getByTestId('keys-loading')).toBeVisible();
	await apiCall(page, 'releaseKeysHold');
	await expect(host.getByTestId('keys-loading')).toHaveCount(0);
	await expect(host.getByTestId('key-row')).toHaveCount(20);
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 12. Sources tab: one-time secret modal — copy, warning, focus, zero traces
// ---------------------------------------------------------------------------

test('sources tab: minting shows the plaintext once, copies it, and close clears every trace', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	await host.getByTestId('tab-sources').click();
	await expect(host.getByTestId('key-row')).toHaveCount(20);

	// A blank label never reaches the wire: the create call is not recorded.
	const callsBeforeBlank = (await apiState(page)).calls.length;
	await host.getByTestId('keys-create').click();
	expect((await apiState(page)).calls.length).toBe(callsBeforeBlank);
	await expect(host.getByTestId('issued-key-modal')).toHaveCount(0);

	// Mint a GLOBAL key with an optional expiry: the wire payload carries the
	// trimmed label + ISO expiry and OMITS server_id (§6: omission = global).
	await host.getByTestId('keys-new-label').fill('host-monitor-2');
	await host.getByTestId('keys-new-expires').fill('2027-01-01T00:00');
	await host.getByTestId('keys-create').click();
	await expect(host.getByTestId('issued-key-modal')).toBeVisible();

	const { calls } = await apiState(page);
	const mints = callsOf(calls, 'createIngestKey');
	expect(mints).toHaveLength(1);
	const expectedExpiry = new Date('2027-01-01T00:00').toISOString();
	expect(mints[0].payload).toEqual({ label: 'host-monitor-2', expires_at: expectedExpiry });

	// The one-time modal: warning, the §7 publish example with the
	// PLACEHOLDER (never the secret), and the plaintext exactly here.
	const modal = host.getByTestId('issued-key-modal');
	await expect(modal.getByTestId('issued-key-warning')).toContainText('will not be shown again');
	const curlText = (await modal.getByTestId('issued-key-curl').textContent()) ?? '';
	expect(curlText).toContain('$MAI_INGEST_KEY');
	expect(curlText).toContain('notifications/ingest');
	const plaintext = (await modal.getByTestId('issued-key-value').textContent()) ?? '';
	expect(plaintext).toMatch(/^mai-k-/);
	expect(curlText).not.toContain(plaintext);

	// Focus moved INTO the dialog on open.
	expect(await page.evaluate(() => document.activeElement?.getAttribute('data-testid'))).toBe(
		'issued-key-modal'
	);

	// Copy: the exact secret reaches the clipboard; the feedback confirms.
	await host.getByTestId('issued-key-copy').click();
	expect(await page.evaluate(() => window.__maiClipboardWrites)).toEqual([plaintext]);
	await expect(modal.getByTestId('issued-key-copied')).toHaveText('Copied to clipboard.');

	// Close: the modal unmounts and the secret is absent from the DOM and
	// from localStorage/sessionStorage (it was never persisted anywhere).
	await host.getByTestId('issued-key-close').click();
	await expect(host.getByTestId('issued-key-modal')).toHaveCount(0);
	const absence = await page.evaluate(() => ({
		html: document.documentElement.outerHTML,
		local: JSON.stringify(localStorage),
		session: JSON.stringify(sessionStorage),
		focused: document.activeElement?.getAttribute('data-testid') ?? null
	}));
	expect(absence.html).not.toContain(plaintext);
	expect(absence.local).not.toContain(plaintext);
	expect(absence.session).not.toContain(plaintext);
	// Focus management on close: back to the create control that triggered it.
	expect(absence.focused).toBe('keys-create');

	// The refreshed durable list carries the minted key: newest first,
	// Global scope, the mint receipt's metadata.
	await expect(host.getByTestId('key-row').nth(0).getByTestId('key-label')).toHaveText(
		'host-monitor-2'
	);
	await expect(host.getByTestId('key-row').nth(0).getByTestId('key-scope')).toHaveText('Global');
	await expect(host.getByTestId('key-row')).toHaveCount(20);
	const { calls: finalCalls } = await apiState(page);
	const finalKeyLists = callsOf(finalCalls, 'listIngestKeys');
	expect(finalKeyLists[finalKeyLists.length - 1].payload).toEqual({ limit: 20, offset: 0 });
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 13. Sources tab: mint scope select — Global + enrolled servers, rendered scope
// ---------------------------------------------------------------------------

test('sources tab: scope select offers Global plus enrolled servers and a scoped key renders its server', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	await host.getByTestId('tab-sources').click();
	await expect(host.getByTestId('key-row')).toHaveCount(20);

	// The mint Scope select: Global first, then enrolled servers in the
	// deterministic (name, id) order the profiles route answers.
	const scopeSelect = host.getByTestId('keys-new-scope');
	expect(await scopeSelect.locator('option').allTextContents()).toEqual([
		'Global',
		SERVER_A_NAME,
		SERVER_B_NAME
	]);

	// A scoped mint carries the explicit server_id and NO expiry field.
	await host.getByTestId('keys-new-label').fill('beta-publisher');
	await scopeSelect.selectOption(SERVER_B);
	await host.getByTestId('keys-create').click();
	await expect(host.getByTestId('issued-key-modal')).toBeVisible();
	let { calls } = await apiState(page);
	const mints = callsOf(calls, 'createIngestKey');
	expect(mints).toHaveLength(1);
	expect(mints[0].payload).toEqual({ label: 'beta-publisher', server_id: SERVER_B });

	// The refreshed row renders the enrolled DISPLAY name, not the raw id.
	await host.getByTestId('issued-key-close').click();
	await expect(host.getByTestId('issued-key-modal')).toHaveCount(0);
	await expect(host.getByTestId('key-row').nth(0).getByTestId('key-label')).toHaveText(
		'beta-publisher'
	);
	await expect(host.getByTestId('key-row').nth(0).getByTestId('key-scope')).toHaveText(
		SERVER_B_NAME
	);

	// Back to Inbox: the retained page re-renders WITHOUT a refetch — the
	// inbox state survives the tab switch (the seam owns no extra loads).
	await host.getByTestId('tab-inbox').click();
	await expect(host.getByTestId('notice-row')).toHaveCount(50);
	const { calls: finalCalls } = await apiState(page);
	expect(callsOf(finalCalls, 'listInboxNotifications')).toHaveLength(1);
	expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// 14. Sources tab: revoke flips the row per receipt; failures surface envelopes
// ---------------------------------------------------------------------------

test('sources tab: revoke flips the row per receipt and failures surface envelopes', async ({
	page
}) => {
	const host = panel(page);
	await expect(host.getByTestId('notifications-root')).toBeVisible();
	await host.getByTestId('tab-sources').click();
	await expect(host.getByTestId('key-row')).toHaveCount(20);

	// The already-revoked seed row exposes NO revoke control.
	await expect(host.getByTestId('key-row').nth(2).getByTestId('key-revoke')).toHaveCount(0);

	// Active seed row 0: the inline confirm gates the destructive call;
	// canceling records nothing and leaves the row untouched.
	const row = host.getByTestId('key-row').nth(0);
	await expect(row.getByTestId('key-status')).toHaveText('active');
	await row.getByTestId('key-revoke').click();
	await expect(row.getByTestId('key-revoke-confirm')).toBeVisible();
	await row.getByTestId('key-revoke-cancel').click();
	await expect(row.getByTestId('key-revoke-confirm')).toHaveCount(0);
	let { calls } = await apiState(page);
	expect(callsOf(calls, 'revokeIngestKey')).toHaveLength(0);

	// Confirm: the receipt gates the durable refetch, and the ROW flips to
	// the server's own stamp — never optimistic.
	await row.getByTestId('key-revoke').click();
	await row.getByTestId('key-revoke-confirm').click();
	await expect(row.getByTestId('key-status')).toHaveText('revoked');
	await expect(row).toHaveAttribute('data-revoked', 'true');
	await expect(row.getByTestId('key-revoked-at')).toHaveText(REVOKE_STAMP);
	({ calls } = await apiState(page));
	const revokes = callsOf(calls, 'revokeIngestKey');
	expect(revokes).toHaveLength(1);
	expect(revokes[0].args).toEqual([keyIdOf(0)]);
	const keyLists = callsOf(calls, 'listIngestKeys');
	expect(keyLists.length).toBeGreaterThanOrEqual(2);
	expect(keyLists[keyLists.length - 1].payload).toEqual({ limit: 20, offset: 0 });

	// Revoke failure: the envelope surfaces, the row NEVER flips.
	await apiCall(page, 'failNextRevokeKey', [
		{ status: 503, code: 'service_unavailable', message: 'revoke refused by the store' }
	]);
	const otherRow = host.getByTestId('key-row').nth(1);
	await otherRow.getByTestId('key-revoke').click();
	await otherRow.getByTestId('key-revoke-confirm').click();
	await expect(host.getByTestId('keys-problem')).toBeVisible();
	await expect(host.getByTestId('keys-problem')).toHaveAttribute(
		'data-error-code',
		'service_unavailable'
	);
	await expect(host.getByTestId('keys-problem')).toContainText('revoke refused');
	await expect(otherRow.getByTestId('key-status')).toHaveText('active');
	await expect(otherRow).toHaveAttribute('data-revoked', 'false');

	// Create failure: the envelope surfaces by the form, no modal mounts and
	// no plaintext ever reaches the DOM.
	await apiCall(page, 'failNextCreateKey', [
		{ status: 422, code: 'validation_failed', message: 'label conflicts with an existing key' }
	]);
	await host.getByTestId('keys-new-label').fill('dupe-label');
	await host.getByTestId('keys-create').click();
	await expect(host.getByTestId('keys-create-error')).toBeVisible();
	await expect(host.getByTestId('keys-create-error')).toHaveAttribute(
		'data-error-code',
		'validation_failed'
	);
	await expect(host.getByTestId('keys-create-error')).toContainText('label conflicts');
	await expect(host.getByTestId('issued-key-modal')).toHaveCount(0);
	expect(await page.evaluate(() => document.body.textContent ?? '')).not.toContain('mai-k-');
	expect(pageErrors).toEqual([]);
});
