/**
 * Feature-owned panel entry — campaign `notification-inbox`, task `nbi-fe-panel`
 * (panel slug `notifications`; windowed-workspace ADR-0001 §7.1).
 *
 * The unified notification inbox replaces per-server attention surfaces as the
 * ONE fleet-wide read surface (docs/architecture/notification-inbox.md):
 *
 *  - the global inbox page (`GET /api/v1/notifications`) under the canonical
 *    filters with honest bounded pagination (AGENTS.md invariant);
 *  - the unread count under the same filters (badge/drawer truth);
 *  - the idempotent read/unread stamps (`ack` / `unack` — evidence preserved,
 *    nothing deleted) rendered ONLY from the receipts the routes answer;
 *  - mark-all-read (receipt-counted, wired into UI by `nbi-fe-filters`);
 *  - the wakeup-only SSE stream (`GET /api/v1/notifications/events`): the
 *    panel treats identity frames as a refetch SIGNAL and never reads
 *    content from them — every byte shown still comes from the durable reads;
 *  - ingest-key management (Sources tab of `nbi-fe-sources`) and
 *    `listProfiles` for server display-name resolution (group headers now;
 *    the server filter/scope Select of the sibling tasks).
 *
 * The window is GLOBAL: the panel reads no pinned server anywhere; a server
 * filter inside the panel is an explicit query param (C01 rule 2 — the window
 * itself stays fleet-wide). The whole notification surface of the composed
 * client (`MaiApiClient`) is wired into the typed services bag 1:1 so the
 * sibling tasks (`nbi-fe-filters`, `nbi-fe-sources`) and the shell
 * (`nbi-fe-shell`) consume this entry instead of restating it.
 *
 * Wire types come only from the reviewed generated client (`schema.generated.ts`
 * via `client.ts`) — never restated field shapes.
 */

import type {
	AckResult,
	AlertOrigin,
	AlertView,
	CreateIngestKeyBody,
	IngestKeyPageView,
	IssuedIngestKeyView,
	MarkAllReadResult,
	NotificationFilterQuery,
	NotificationListQuery,
	NotificationPage,
	NotificationWakeup,
	ProfileResponse,
	RevokeIngestKeyResult,
	SubscribeNotificationEventsOptions,
	UnreadCountView
} from '../../api/client.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const NOTIFICATIONS_PANEL_ID = 'notifications';

// ---------------------------------------------------------------------------
// Generated wire shapes (schema.generated.ts via client.ts; never restated)
// ---------------------------------------------------------------------------

export type {
	/** One inbox notice (`0017 notification_alerts` row). */
	AlertView,
	/** Notice origin vocabulary (`finding | job_result | external`). */
	AlertOrigin,
	/** The ack/unack receipt (the notice view after the idempotent stamp). */
	AckResult,
	/** One bounded global inbox page (canonical filters, newest first). */
	NotificationPage,
	/** The badge/drawer count: unread notices under the exact canonical filters. */
	UnreadCountView,
	/** Mark-all-read receipt: the database's own rowcount, never a guess. */
	MarkAllReadResult,
	/** One bounded ingest-key metadata page (never key material). */
	IngestKeyPageView,
	/** Mint payload: label (1..64), optional server scope, optional expiry. */
	CreateIngestKeyBody,
	/** The mint receipt — plaintext carried EXACTLY ONCE, then never again. */
	IssuedIngestKeyView,
	/** Revoke receipt (`revoked: false` deliberately covers unknown/already-revoked). */
	RevokeIngestKeyResult,
	/** One stored server profile (group-header display names, key scopes). */
	ProfileResponse,
	/**
	 * Canonical inbox filter surface shared by list/count/mark-all (R4-F2):
	 * `server_id` and `scope: 'global'` are mutually exclusive;
	 * `include_global` travels only beside an explicit `server_id`.
	 */
	NotificationFilterQuery,
	/** Bounded-page query: the canonical filters plus `limit`/`offset`. */
	NotificationListQuery,
	/** One identity-only SSE wakeup (`created`/`updated`/`resync` — no content). */
	NotificationWakeup,
	/** Options of the wakeup-only subscription (client binding of this base URL). */
	SubscribeNotificationEventsOptions
} from '../../api/client.js';


/**
 * Bounded-page query of the ingest-key list route (producer default page,
 * explicit max). Structurally identical to the client's shared `PageQuery`
 * (which is runs-vocabulary in its own module) without importing across
 * feature modules.
 */
export interface IngestKeyPageQuery {
	/** Page size; producer default applies when omitted. */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

// ---------------------------------------------------------------------------
// Services bag (mirrors the reviewed client surface 1:1; opener supplies it)
// ---------------------------------------------------------------------------

/**
 * The full notification surface this feature consumes, injected via the
 * panel services bag and satisfied 1:1 from the composed `MaiApiClient`.
 * Reads are session-checked server-side; the ack/unack/mark-all mutations are
 * additionally origin+CSRF checked (auth ADR §5) — the bag never weakens
 * that, and the ingest-key plaintext never persists anywhere client-side
 * (C03 rule 6). Every inbox read is fleet-wide under EXPLICIT canonical
 * filters — there is no ambient scope anywhere in this surface (C01 rule 2).
 */
export interface NotificationsApi {
	/** GET /api/v1/notifications — bounded unified-inbox page under the canonical filters. */
	listInboxNotifications(query?: NotificationListQuery): Promise<NotificationPage>;
	/** GET /api/v1/notifications/unread-count — unread total under the same filters. */
	getUnreadNotificationCount(query?: NotificationFilterQuery): Promise<UnreadCountView>;
	/** POST /api/v1/notifications/{alert_id}/ack — idempotent read stamp. */
	acknowledgeInboxNotification(alertId: string): Promise<AckResult>;
	/** POST /api/v1/notifications/{alert_id}/unack — idempotent unread stamp. */
	unacknowledgeInboxNotification(alertId: string): Promise<AckResult>;
	/** POST /api/v1/notifications/mark-all-read — one update under the same filters. */
	markAllNotificationsRead(query?: NotificationFilterQuery): Promise<MarkAllReadResult>;
	/**
	 * GET /api/v1/notifications/events — wakeup-only SSE binding of the
	 * client's base URL. Identity/resync frames reach `onWakeup`; the bounded
	 * self-close fires `onStreamEnd` (refetch + resubscribe there). No cursor.
	 */
	subscribeNotificationEvents(options: SubscribeNotificationEventsOptions): Promise<void>;
	/** GET /api/v1/servers — every stored profile, deterministic order (display names). */
	listProfiles(): Promise<{ servers: ProfileResponse[] }>;
	/** GET /api/v1/ingest-keys — bounded metadata page (never key material). */
	listIngestKeys(page?: IngestKeyPageQuery): Promise<IngestKeyPageView>;
	/** POST /api/v1/ingest-keys — mints; plaintext arrives ONCE as `key`. */
	createIngestKey(payload: CreateIngestKeyBody): Promise<IssuedIngestKeyView>;
	/** POST /api/v1/ingest-keys/{key_id}/revoke — effective on the next ingest. */
	revokeIngestKey(keyId: string): Promise<RevokeIngestKeyResult>;
}

/**
 * The host's ONE sequenced server-filter handoff — the pre-authorized
 * cross-panel seam that lets the OverviewPanel's «View in Inbox →» control
 * drive THIS fleet-wide window (the window itself stays global; the server
 * filter is an explicit canonical query param, C01 rule 2 — the seam never
 * pins a scope). `read()` returns the latest request `{serverId, seq}` or
 * `null`; `seq` strictly increases per request so the panel can apply each
 * handoff exactly once (including when the singleton window is already
 * mounted). `consume(seq)` acknowledges one applied request so the host may
 * clear it — a later plain open of the window starts unfiltered.
 */
export interface NotificationsFilterHandoff {
	/** Latest handoff request, or `null` when none is pending. */
	read(): { serverId: string; seq: number } | null;
	/** Acknowledge one applied request (idempotent per seq). */
	consume(seq: number): void;
}

/** Typed services bag the notifications panel requires from its opener. */
export interface NotificationsServices {
	readonly api: NotificationsApi;
	/**
	 * Optional handoff seam (the shell wires it; unwired hosts pass `null`
	 * and the panel fails closed to the plain fleet-wide inbox).
	 */
	readonly handoff: NotificationsFilterHandoff | null;
}

/** The feature entry for composition into the app's one panel registry. */
export function notificationsEntry(): PanelEntryInput {
	return {
		panel_kind: NOTIFICATIONS_PANEL_ID,
		title: 'Notifications',
		scope_types: ['global'],
		component: () =>
			import('../../panels/NotificationsPanel.svelte').then((module) => module.default),
		default_geometry: { x: 48, y: 48, width: 760, height: 560 },
		min_geometry: { x: 0, y: 0, width: 420, height: 320 },
		// One desktop holds ONE inbox window: the surface is fleet-wide and a
		// second window would only duplicate the same global reads.
		singleton: true,
		capabilities_note:
			'Reads the fleet-wide unified inbox (bounded page, Load more, page-bound grouping, ' +
			'read/unread toggles, live wakeup refresh); registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `notificationsServices(slice, handoff)` into its panel hosts; `nbi-fe-shell`
 * composes the slice from the one `MaiApiClient` and owns the ONE sequenced
 * handoff seam). Freezing keeps the bag read-only from the panel's side; the
 * panel still fails closed when the bag is missing or incomplete.
 */
export function notificationsServices(
	api: NotificationsApi,
	handoff: NotificationsFilterHandoff | null = null
): NotificationsServices {
	return Object.freeze({ api, handoff });
}
