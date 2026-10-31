/**
 * Feature-owned panel entry — card `mai-05-web-jobs` (P05, service `web`).
 *
 * This module is the ONE registration point of the jobs/schedules management
 * panel (windowed ADR §7.1: a fixed trusted component map; registration
 * grants no permissions). It owns:
 *
 *  - the stable panel slug and the typed wire shapes it consumes (generated
 *    schema only — no invented endpoints, C08 rule 8);
 *  - the typed services bag the panel requires, mirroring the reviewed
 *    producers 1:1 (`05-jobs-api` routes, `backend/src/mai/api/jobs.py` and
 *    `backend/src/mai/api/schedules.py`);
 *  - the concrete SSE binding of the one event-stream route (C08 rule 7
 *    open item: transport = SSE, replay cursor = `Last-Event-ID`, settled by
 *    `05-jobs-api`; this panel is the consumer). The URL builder and the
 *    stream call are composed here once; the panel itself stays transport-
 *    shaped but never hardcodes the path.
 *
 * No module-level mutable state; safe to evaluate per SSR request (ADR §5
 * rule 4). Composition passes `jobsServices({ …createMaiApiClient(),
 * streamRunEvents: browserRunEventStream })` — the base client grows the
 * jobs/schedules transport methods; instances still share nothing.
 */

import type { components } from '../../api/schema.generated.js';
import type {
	SubscribeEventsOptions,
	TagAssignBody,
	TagCatalogQuery,
	TagEntityTagsQuery,
	TagRemovalOptions
} from '../../api/client.js';
import { createMaiApiClient, subscribeEvents } from '../../api/client.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const JOBS_PANEL_KIND = 'jobs';

/** Generated wire shape: one bounded, explicitly server-scoped run page. */
export type RunPage = components['schemas']['RunPage'];
/** Generated wire shape: one run: identity binding plus replayed facade state. */
export type RunView = components['schemas']['RunView'];
/** Generated wire shape: one run plus its durable step rows. */
export type RunDetail = components['schemas']['RunDetail'];
/** Generated wire shape: explicit tree cancellation body (engine guard rule 4). */
export type CancelBody = components['schemas']['CancelBody'];
/** Generated wire shape: KNOWN engine outcome or an honest pending. */
export type CancelView = components['schemas']['CancelView'];
/** Generated wire shape: one persisted schedule record. */
export type ScheduleView = components['schemas']['ScheduleView'];
/** Generated wire shape: schedule create body (strict; server re-validates). */
export type ScheduleCreateBody = components['schemas']['ScheduleCreateBody'];
/** Generated wire shape: pause/resume ONLY (the definition is frozen). */
export type ScheduleUpdateBody = components['schemas']['ScheduleUpdateBody'];
/** Generated wire shape: one bounded schedule page. */
export type SchedulePage = components['schemas']['SchedulePage'];
/** Generated wire shape: one bounded global tag catalog page (HT-W6B). */
export type TagPage = components['schemas']['TagPage'];
/** Generated wire shape: one catalog row (the stored name IS the identity). */
export type TagView = components['schemas']['TagView'];
/** Generated wire shape: one run-tag association row as seen from the run. */
export type EntityTagView = components['schemas']['EntityTagView'];
/** Generated wire shape: one bounded tag page of one job run (HT-W6B). */
export type JobTagsPage = components['schemas']['JobTagsPage'];

/**
 * Optional bounded-page query shared by the list routes (C01 rule 5). The
 * jobs runs list additionally carries the reviewed W1 filters; the server
 * applies them BEFORE pagination (the pre-slice `total` is filtered).
 */
export interface PageQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
	/**
	 * Runs list only: case-insensitive substring search over run_id, label
	 * and model_profile (W1). Never sent for other collections.
	 */
	q?: string;
	/**
	 * Runs list only: inclusive lower `submitted_at` bound as an explicit
	 * ISO instant (W1). The Today preset translates browser-local midnight
	 * to this bound client-side; ambiguous local date text is never sent.
	 */
	date_from?: string;
	/** Runs list only: inclusive upper `submitted_at` bound (ISO instant). */
	date_to?: string;
	/**
	 * Runs list only (HT-W6B): repeated catalog tag ids, at most 50, matched
	 * per `tagMatch` BEFORE rows+COUNT and pagination. The client never sends
	 * more than 50; the server rejects a longer list.
	 */
	tagIds?: string[];
	/** Runs list only (HT-W6B): `any` (at least one, default) or `all` (every). */
	tagMatch?: 'any' | 'all';
}

/**
 * Options of the one SSE subscription (the generic `SubscribeEventsOptions`
 * shape of the reviewed client primitive): `cursor` is the last seen event
 * `seq` and travels as the standard SSE `Last-Event-ID` header; `signal`
 * aborts the stream (hide/close/unmount release path, ADR §7.2); `onEvent`
 * receives each raw frame in stream order for the panel to narrow and
 * deduplicate.
 */
export type RunEventStreamOptions = SubscribeEventsOptions;

/**
 * The jobs/schedules API slice this panel consumes, injected via the panel
 * services bag (satisfied by the composed client once it grows the P05
 * transport methods). Mirrors the reviewed routes 1:1; read-only routes are
 * session-checked server-side, mutations additionally origin+CSRF-checked
 * (auth ADR §5) — the bag never weakens that.
 */
export interface JobsApi {
	/** GET /api/v1/jobs/runs?server_id=… — explicit scope, never unfiltered (C01 r7). */
	listRuns(serverId: string, page?: PageQuery): Promise<RunPage>;
	/**
	 * OPTIONAL name seam for the pinned scope (W3): resolves ONLY the panel's
	 * pinned `server_id` to the enrolled profile's human name. When absent,
	 * resolution fails, or the bag was composed without it, the panel keeps
	 * the UUID and shows a visible name-unavailable state — jobs data never
	 * depends on it. The narrow shape deliberately hides the full profile.
	 */
	readServerName?(serverId: string): Promise<string>;
	/** GET /api/v1/jobs/runs/{run_id} — replayed state + durable steps. */
	readRun(runId: string): Promise<RunDetail>;
	/** POST /api/v1/jobs/runs/{run_id}/cancel — records the request once. */
	cancelRun(runId: string, payload: CancelBody): Promise<CancelView>;
	/** GET /api/v1/schedules?server_id=… — optional but EXPLICIT scope. */
	listSchedules(serverId: string | null, page?: PageQuery): Promise<SchedulePage>;
	/** POST /api/v1/schedules — durable configuration; target is bound here. */
	createSchedule(payload: ScheduleCreateBody): Promise<ScheduleView>;
	/** PATCH /api/v1/schedules/{schedule_id} — pause/resume only. */
	updateSchedule(scheduleId: string, payload: ScheduleUpdateBody): Promise<ScheduleView>;
	/** DELETE /api/v1/schedules/{schedule_id} — row + fires cascade. */
	deleteSchedule(scheduleId: string): Promise<void>;
	/**
	 * GET /api/v1/jobs/runs/{run_id}/events — authenticated, origin-checked
	 * SSE subscription with `Last-Event-ID` replay (C08 rule 7). Resolves
	 * when the server closes its bounded stream or `signal` aborts; rejects
	 * on HTTP/network failure. Read-only: a disconnected browser cancels
	 * nothing (windowed ADR §7).
	 */
	streamRunEvents(runId: string, options: RunEventStreamOptions): Promise<void>;
	/**
	 * HT-W6B tag surface. OPTIONAL on the narrow opener bag (existing
	 * compositions keep typechecking); `jobsServices` ALWAYS materializes
	 * all four from the reviewed base client before the panel sees the bag,
	 * so the panel's runtime contract (`JobsPanelApi`) requires them and
	 * fails closed when a bag was not composed through this entry.
	 */
	/** GET /api/v1/tags — bounded global catalog page with the name filter (HT-W6B). */
	listTags?(query?: TagCatalogQuery): Promise<TagPage>;
	/**
	 * GET /api/v1/jobs/runs/{run_id}/tags?server_id=… — the run's bounded tag
	 * page; the explicit scope must name the run's own server (C01 rule 2).
	 */
	listRunTags?(
		runId: string,
		serverId: string,
		page?: TagEntityTagsQuery
	): Promise<JobTagsPage>;
	/** POST /api/v1/jobs/runs/{run_id}/tags — idempotent user assignment (strict body scope). */
	assignRunTag?(runId: string, payload: TagAssignBody): Promise<void>;
	/**
	 * DELETE /api/v1/jobs/runs/{run_id}/tags/{tag_id}?server_id=… — idempotent
	 * removal; the run's final tag refuses (409 `last_tag`) unless the
	 * same-transaction `replacementTagId` re-classifies it.
	 */
	removeRunTag?(runId: string, tagId: string, options: TagRemovalOptions): Promise<void>;
}

/**
 * The panel-facing jobs API: the opener bag with the HT-W6B tag methods
 * REQUIRED (every composition through `jobsServices` provides them, from
 * the opener or from the reviewed base client).
 */
export type JobsPanelApi = JobsApi &
	Required<Pick<JobsApi, 'listTags' | 'listRunTags' | 'assignRunTag' | 'removeRunTag'>>;

/** Typed services bag the jobs panel requires from its opener. */
export interface JobsServices {
	readonly api: JobsApi;
}

/**
 * The reviewed producer path of the run event stream (`05-jobs-api`); the
 * one place the concrete URL exists client-side. The path is pinned by the
 * generated schema operation `stream_events_api_v1_jobs_runs__run_id__get`.
 */
export function runEventsUrl(runId: string): string {
	return `/api/v1/jobs/runs/${encodeURIComponent(runId)}/events`;
}

/**
 * Browser composition of the bag's `streamRunEvents`: the reviewed generic
 * SSE primitive over the concrete producer URL. Authenticated via cookies,
 * replay cursor carried as `Last-Event-ID` (C08 rule 7).
 */
export function browserRunEventStream(runId: string, options: RunEventStreamOptions): Promise<void> {
	return subscribeEvents(runEventsUrl(runId), options);
}

/** The feature entry for composition into the app's one panel registry. */
export function jobsEntry(): PanelEntryInput {
	return {
		panel_kind: JOBS_PANEL_KIND,
		title: 'Jobs',
		scope_types: ['server'],
		component: () => import('../../panels/JobsPanel.svelte').then((module) => module.default),
		default_geometry: { x: 96, y: 96, width: 720, height: 560 },
		min_geometry: { x: 0, y: 0, width: 460, height: 360 },
		singleton: true,
		capabilities_note:
			'Reads replayed run state, watches the persisted event stream and manages ' +
			'schedules for the pinned target; cancel records a request; registration ' +
			'grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `jobsServices({ …baseClientJobsMethods, streamRunEvents: browserRunEventStream })`
 * into its panel hosts). Freezing keeps the bag read-only from the panel's
 * side; the panel still fails closed if the bag is missing or incomplete.
 *
 * HT-W6B: the tag methods MAY be omitted by a narrow opener composition;
 * when absent they are materialized here from the reviewed base client
 * (same transport, same C01 envelope/CSRF semantics — a per-composition
 * client instance, no module-level state), so every panel sees the full
 * `JobsPanelApi` and never a partial tag surface.
 */
export function jobsServices(api: JobsApi): JobsServices {
	const base = createMaiApiClient();
	const tagSurface: Pick<
		JobsPanelApi,
		'listTags' | 'listRunTags' | 'assignRunTag' | 'removeRunTag'
	> = {
		listTags: api.listTags ?? base.listTags,
		listRunTags: api.listRunTags ?? base.listRunTags,
		assignRunTag: api.assignRunTag ?? base.assignRunTag,
		removeRunTag: api.removeRunTag ?? base.removeRunTag
	};
	return Object.freeze({ api: Object.assign({}, api, tagSurface) as JobsPanelApi });
}
