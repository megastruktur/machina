/**
 * Feature-owned panel entry — card `mai-10-web-dashboard` (P10, service `web`).
 *
 * The overview/attention-inbox panel composes the reviewed monitor evidence
 * surfaces (contracts.md C10 rules 3/4, C08 rule 6, C11) for ONE pinned
 * server:
 *
 *  - monitor freshness from the `10-monitor-api` observation queries
 *    (freshness is derived at request time by the producer — the overview
 *    never recomputes or caches it);
 *  - finding attention + the re-evaluation action (`POST …/findings/evaluate`)
 *    whose result renders ONLY through the reviewed `ObservationDetails`
 *    drawer contract (`10-web-evidence`), never a duplicated detail view;
 *  - notification attention + the evidence-preserving ack action
 *    (`POST …/notifications/{alert_id}/ack` — idempotent, deletes nothing);
 *  - application and job state summaries from the reviewed `08-app-api`
 *    adopted page and the `05-jobs-api` run page, with LINKS (seams) to
 *    their tested panels — the overview owns none of their logic.
 *
 * Live updates ride the ONE reviewed event stream (C08 rule 7): the window
 * may pin a `run` object and the panel treats that run's persisted SSE
 * stream as a change SIGNAL (the same producer composition `05-web-jobs`
 * reviewed: `browserRunEventStream` over `/api/v1/jobs/runs/{run_id}/events`);
 * every byte still comes from the durable HTTP reads — the stream never
 * carries rendered state. There is no monitor SSE endpoint in the reviewed
 * surface and none is invented here (C08 rule 8).
 *
 * Wire types come only from the reviewed generated client (`schema.generated.ts`).
 */

import type { components } from '../../api/schema.generated.js';
import type { SubscribeEventsOptions } from '../../api/client.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const OVERVIEW_PANEL_KIND = 'overview';

// ---------------------------------------------------------------------------
// Generated wire shapes (schema.generated.ts; never restated fields)
// ---------------------------------------------------------------------------

/** Generated wire shape: one bounded observation page for one server. */
export type ObservationPage = components['schemas']['ObservationPage'];
/** Generated wire shape: one collected health sample + request-time freshness. */
export type ObservationView = components['schemas']['ObservationView'];
/** Generated wire shape: one bounded finding page for one server. */
export type FindingPage = components['schemas']['FindingPage'];
/** Generated wire shape: one derived condition (open/resolved + severity). */
export type FindingView = components['schemas']['FindingView'];
/** Generated wire shape: one bounded re-evaluation round outcome. */
export type EvaluateResult = components['schemas']['EvaluateResult'];
/** Generated wire shape: one bounded inbox page for one server. */
export type AlertPage = components['schemas']['AlertPage'];
/** Generated wire shape: one inbox notice (`acked` mirrors the stamp). */
export type AlertView = components['schemas']['AlertView'];
/** Generated wire shape: the ack receipt (the notice view after the stamp). */
export type AckResult = components['schemas']['AckResult'];
/** Generated wire shape: one bounded adopted-application page. */
export type ApplicationPage = components['schemas']['ApplicationPage'];
/** Generated wire shape: one projected adoption record. */
export type ApplicationView = components['schemas']['ApplicationView'];
/** Generated wire shape: one bounded, explicitly server-scoped run page. */
export type RunPage = components['schemas']['RunPage'];
/** Generated wire shape: one run with replayed facade state. */
export type RunView = components['schemas']['RunView'];

/** Bounded-page query shared by the monitor list routes (C01 rule 5). */
export interface OverviewPageQuery {
	/** Page size; server default applies when omitted (producer clamps max). */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

/** Query of the findings list route (attention-ordered by the producer). */
export interface OverviewFindingsQuery extends OverviewPageQuery {
	/** `true` lists open conditions only; omitted lists everything. */
	open?: boolean | null;
}

/** Query of the notification inbox route (newest-first per producer). */
export interface OverviewAlertsQuery extends OverviewPageQuery {
	/** `true` lists unacknowledged notices only (the attention set). */
	unacknowledged?: boolean | null;
}

/** Options of the one SSE subscription (reviewed `05-web-jobs` shape). */
export type OverviewRunEventStreamOptions = SubscribeEventsOptions;

// ---------------------------------------------------------------------------
// Bounded chat-attachment payload (mirrors the reviewed drawer contract 1:1)
// ---------------------------------------------------------------------------

/**
 * Exactly what the chat seam may receive from the overview's evidence
 * drawer: identity + the bounded `observation:<uuid>` / `finding:<uuid>`
 * context reference — never a payload. Structurally identical to the
 * `EvidenceAttachPayload` the reviewed `ObservationDetails` component emits;
 * the seam is the panel's typed vocabulary for its opener.
 */
export interface EvidenceAttachPayload {
	server_id: string;
	kind: 'observation' | 'finding';
	context_ref: string;
}

// ---------------------------------------------------------------------------
// Composition seams (separately implemented surfaces; types only)
// ---------------------------------------------------------------------------

/**
 * The panel-links seam: opening the separately implemented Applications and
 * Jobs panels is THEIR feature surface's job (windowing is host-owned, ADR
 * §7.1). The overview only requests the open. `null`/incomplete fails
 * closed to a visible note — never a fake navigation.
 */
export interface OverviewLinks {
	/** Open the applications inventory for one server. */
	openApplications(serverId: string): void;
	/** Open the runs/schedules management panel for one server. */
	openJobs(serverId: string): void;
	/**
	 * Optional: open (or focus) the ONE fleet-wide `notifications` window with
	 * THIS server's inbox filter pre-selected — the cross-panel hand-off to
	 * the `14-web-notifications` global entry (the filter is the panel's own
	 * explicit query param; the window itself stays fleet-wide, C01 rule 2).
	 * Omitted ⇒ the overview's «View in Inbox →» control fails closed and
	 * does not render (never a fake navigation).
	 */
	openNotifications?(serverId: string): void;
}

/**
 * The chat hand-off seam (the DocumentsPanel pattern): the embedding
 * surface owns the ONE reviewed chat submission seam; the overview hands
 * over only the bounded context reference. `null`/incomplete fails closed —
 * the drawer's attach control renders only when the seam is wired.
 */
export interface OverviewChat {
	/** Hand one bounded evidence reference to the chat surface. */
	attach(payload: EvidenceAttachPayload): void;
}

// ---------------------------------------------------------------------------
// Services bag (mirrors the reviewed routes 1:1; opener supplies it)
// ---------------------------------------------------------------------------

/**
 * The monitoring/catalogue/jobs read+action slice this panel consumes,
 * injected via the panel services bag (satisfied by the composed client
 * once it grows the transport methods). Mirrors the reviewed routes 1:1;
 * reads are session-checked server-side, the evaluate/ack mutations are
 * additionally origin+CSRF checked (auth ADR §5) — the bag never weakens
 * that. Every method names its scope explicitly; there is no module-level
 * or client-level "current server" anywhere (C01 rule 2, NT5).
 */
export interface OverviewApi {
	/** GET /api/v1/monitoring/servers/{id}/observations — newest-first page. */
	listObservations(serverId: string, page?: OverviewPageQuery): Promise<ObservationPage>;
	/** GET /api/v1/monitoring/servers/{id}/findings — attention-ordered page. */
	listFindings(serverId: string, query?: OverviewFindingsQuery): Promise<FindingPage>;
	/** POST /api/v1/monitoring/servers/{id}/findings/evaluate — re-run the REAL evaluator. */
	evaluateFindings(serverId: string): Promise<EvaluateResult>;
	/** GET /api/v1/monitoring/servers/{id}/notifications — the inbox page. */
	listNotifications(serverId: string, query?: OverviewAlertsQuery): Promise<AlertPage>;
	/** POST /api/v1/monitoring/servers/{id}/notifications/{alert_id}/ack — idempotent stamp. */
	ackNotification(serverId: string, alertId: string): Promise<AckResult>;
	/** GET /api/v1/servers/{id}/applications — one scope's adopted page. */
	listAdoptedApplications(serverId: string, page?: OverviewPageQuery): Promise<ApplicationPage>;
	/** GET /api/v1/jobs/runs?server_id=… — explicit scope, never unfiltered. */
	listRuns(serverId: string, page?: OverviewPageQuery): Promise<RunPage>;
	/**
	 * GET /api/v1/jobs/runs/{run_id}/events — the ONE reviewed SSE seam
	 * (C08 rule 7), composed browser-side by `05-web-jobs`. Resolves when
	 * the server closes its bounded stream or `signal` aborts; rejects on
	 * HTTP/network failure. Read-only: the stream never submits anything.
	 */
	streamRunEvents(runId: string, options: OverviewRunEventStreamOptions): Promise<void>;
}

/** Typed services bag the overview panel requires from its opener. */
export interface OverviewServices {
	readonly api: OverviewApi;
	/**
	 * The panel-links seam (Applications/Jobs openers). `null` fails closed:
	 * the overview renders the honest unavailable note and never fakes a
	 * navigation.
	 */
	readonly links: OverviewLinks | null;
	/**
	 * The chat hand-off seam for the evidence drawer. `null` fails closed:
	 * the attach control simply renders only when this seam is wired.
	 */
	readonly chat: OverviewChat | null;
}

/** The feature entry for composition into the app's one panel registry. */
export function overviewEntry(): PanelEntryInput {
	return {
		panel_kind: OVERVIEW_PANEL_KIND,
		title: 'Overview',
		scope_types: ['server'],
		// A window may pin ONE run object: that run's persisted event stream
		// then drives the live-refresh signal (C08 rule 7). No other object
		// kind has overview meaning.
		scope_object_kinds: ['run'],
		component: () => import('../../panels/OverviewPanel.svelte').then((module) => module.default),
		default_geometry: { x: 40, y: 40, width: 920, height: 640 },
		min_geometry: { x: 0, y: 0, width: 520, height: 380 },
		// Singleton per full pinned scope: one desktop holds one overview per
		// server (a run-pinned variant is its own scope key).
		singleton: true,
		capabilities_note:
			'Summarizes the pinned server\'s monitor freshness, open findings, notification ' +
			'inbox and application/job state; acknowledges notices and re-evaluates findings; ' +
			'registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `overviewServices({ …monitoringAndCatalogueAndJobsMethods }, links, chat)`
 * into its panel hosts). Freezing keeps the bag read-only from the panel's
 * side; the panel still fails closed if the bag is missing or incomplete.
 */
export function overviewServices(
	api: OverviewApi,
	links: OverviewLinks | null = null,
	chat: OverviewChat | null = null
): OverviewServices {
	return Object.freeze({ api, links, chat });
}
