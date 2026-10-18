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
import type { SubscribeEventsOptions } from '../../api/client.js';
import { subscribeEvents } from '../../api/client.js';
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

/** Optional bounded-page query shared by the list routes (C01 rule 5). */
export interface PageQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
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
}

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
 */
export function jobsServices(api: JobsApi): JobsServices {
	return Object.freeze({ api });
}
