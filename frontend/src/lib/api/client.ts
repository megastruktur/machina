/**
 * MAI typed API client boundary (C01/C08) — hand-written transport over the
 * generated schema. Payload types come exclusively from
 * `./schema.generated.ts`; endpoint methods here never restate field shapes.
 *
 * Schema generation (the one documented command; rerun from the worktree
 * root whenever a backend wire model changes, then `git diff` must be empty
 * on a no-drift tree):
 *
 *     export_path=/tmp/mai-fxt-02-api-client/openapi.json && \
 *     mkdir -p "$(dirname "$export_path")" && \
 *     (cd backend && uv run python -c "import json,sys; from mai.api.app import \
 * create_app; json.dump(create_app().openapi(), open(sys.argv[1],'w'), indent=2)" \
 * "$export_path") && \
 *     (cd frontend && pnpm dlx openapi-typescript@7.13.0 "$export_path" \
 * -o src/lib/api/schema.generated.ts)
 *
 * Contract invariants enforced here (contracts.md C01/C08; auth ADR §4–§5):
 * - The principal is the `mai_session` HttpOnly cookie (C08 rule 1); this
 *   client never reads, stores or transmits the session token anywhere but
 *   the browser cookie jar (`credentials: 'include'`), and nothing is
 *   persisted to localStorage/sessionStorage.
 * - Mutating requests carry double-submit `x-csrf-token` == `mai_csrf`
 *   cookie (C08 rule 2) whenever the session-bearing boundary requires it;
 *   a missing token fails closed client-side before any bytes are sent.
 *   The G1 setup-state routes (`bootstrap`, `login`) run before any cookie
 *   exists and are exact-origin-protected instead (auth ADR §3/§8), so the
 *   client attaches their token only when one is already available.
 * - Errors are the C01 structured envelope `{error: {code, message,
 *   request_id, fields}}`; anything unparsable collapses to a generic
 *   status-derived `ApiError` — raw bodies are never surfaced to UI code.
 * - Every server-scoped operation takes its explicit `server_id` argument
 *   (C01 rule 2); there is no module-level or client-level "current server"
 *   anywhere in this module.
 * - Instances are self-contained: no module-level mutable state, so no SSR
 *   singleton can share state between requests (windowed ADR D4; §5 rule 2).
 *   Browser APIs are touched only inside instance methods, never at module
 *   top level.
 *
 * Event cursor transport (C08 rule 7 / C05 rule 4): `subscribeEvents` is a
 * generic SSE read primitive. The concrete jobs endpoint URL and its wire
 * format are owned by `05-jobs-api`/`05-web-jobs` (the one open C08 item);
 * this module commits only to SSE `Last-Event-ID` as the replay-cursor
 * carrier and to the frozen event envelope shape (`MaiRunEvent`). It never
 * invents an endpoint path.
 */

import type { components, operations } from './schema.generated';
// Producer hand-backs the OpenAPI exporter declares `unknown` (plain
// JSONResponse routes) and the bounded-page query vocabulary: reuse the
// reviewed entry-module mirrors verbatim. Type-only imports — erased at
// runtime, so no runtime cycle with the entry modules exists.
import type {
	PageQuery
} from '../workspace/entries/05-web-jobs.js';
import type {
	DocNamespace,
	DocumentHeadView,
	DocumentPage,
	DocumentView,
	DocsPageQuery,
	GenerateView,
	IndexStatus,
	ReconcileReport,
	ResearchReport,
	RetrievalResult
} from '../workspace/entries/07-web-docs.js';
import type { ApplicationsPageQuery } from '../workspace/entries/08-web-apps.js';
import type {
	OverviewAlertsQuery,
	OverviewFindingsQuery,
	OverviewPageQuery
} from '../workspace/entries/10-web-dashboard.js';
import type {
	MemoryDeleteDocument,
	MemoryExportDocument,
	MemoryRecallDocument,
	MemoryReflectDocument,
	MemoryRetainDocument,
	MemoryStatusDocument
} from '../workspace/entries/11-web-memory-models.js';

/** Frozen wire names (auth ADR §4–§5; mirrors backend mai.api.auth). */
export const SESSION_COOKIE = 'mai_session';
export const CSRF_COOKIE = 'mai_csrf';
export const CSRF_HEADER = 'x-csrf-token';

/** C01 rule 3 stable error codes observed in the integrated P02 surface. */
export type KnownApiErrorCode =
	| 'unauthenticated'
	| 'csrf_rejected'
	| 'origin_rejected'
	| 'request_rejected'
	| 'throttled'
	| 'conflict'
	| 'validation_failed'
	| 'not_found'
	| 'method_not_allowed'
	| 'unavailable'
	| 'internal_error';

export type ApiErrorBody = components['schemas']['ErrorBody'];
export type PasswordBody = components['schemas']['PasswordBody'];
export type ProfileCreate = components['schemas']['ProfileCreate'];
export type ProfileEdit = components['schemas']['ProfileEdit'];
export type ProfileMutationResult = components['schemas']['ProfileMutationResult'];
export type ProfileResponse = components['schemas']['ProfileResponse'];

/** Generated wire shapes consumed by the feature panels (P03–P12). */
export type AckResult = components['schemas']['AckResult'];
export type AdoptBody = components['schemas']['AdoptBody'];
export type AdoptionCreated = components['schemas']['AdoptionCreated'];
export type AlertPage = components['schemas']['AlertPage'];
export type ApprovalView = components['schemas']['ApprovalView'];
export type ApplicationPage = components['schemas']['ApplicationPage'];
export type BindingView = components['schemas']['BindingView'];
export type CancelBody = components['schemas']['CancelBody'];
export type CancelView = components['schemas']['CancelView'];
export type ChallengeBody = components['schemas']['ChallengeBody'];
export type ChallengeOutcomeView = components['schemas']['ChallengeOutcomeView'];
export type ChatCancelBody = components['schemas']['ChatCancelBody'];
export type ChatCreated = components['schemas']['ChatCreated'];
export type ChatPage = components['schemas']['ChatPage'];
export type ChatSubmitBody = components['schemas']['ChatSubmitBody'];
export type ConversationView = components['schemas']['ConversationView'];
export type DecisionBody = components['schemas']['DecisionBody'];
export type DiscoveryView = components['schemas']['DiscoveryView'];
export type DocumentEditBody = components['schemas']['DocumentEditBody'];
export type EvaluateResult = components['schemas']['EvaluateResult'];
export type FindingPage = components['schemas']['FindingPage'];
export type GenerateBody = components['schemas']['GenerateBody'];
export type GrantsConfigView = components['schemas']['GrantsConfigView'];
export type GrantsEditBody = components['schemas']['GrantsEditBody'];
export type GrantsMutationResult = components['schemas']['GrantsMutationResult'];
export type McpDiscoveryReportView = components['schemas']['McpDiscoveryReportView'];
export type ModelConnectionBody = components['schemas']['ModelConnectionBody'];
export type ModelConnectionList = components['schemas']['ModelConnectionList'];
export type ModelConnectionView = components['schemas']['ModelConnectionView'];
export type ModelLocalPreviewBody = components['schemas']['ModelLocalPreviewBody'];
export type ModelLocalPreviewView = components['schemas']['ModelLocalPreviewView'];
export type ModelLocalPullBody = components['schemas']['ModelLocalPullBody'];
export type ModelLocalSubmitBody = components['schemas']['ModelLocalSubmitBody'];
export type ModelMutationResult = components['schemas']['ModelMutationResult'];
export type ModelProbeBody = components['schemas']['ModelProbeBody'];
export type ModelProbeOutcome = components['schemas']['ModelProbeOutcome'];
export type ModelProfileBody = components['schemas']['ModelProfileBody'];
export type ModelProfileList = components['schemas']['ModelProfileList'];
export type ModelProfileView = components['schemas']['ModelProfileView'];
export type ModelSelectionBody = components['schemas']['ModelSelectionBody'];
export type ModelSelectionView = components['schemas']['ModelSelectionView'];
export type ObservationPage = components['schemas']['ObservationPage'];
export type PluginBindingEditBody = components['schemas']['PluginBindingEditBody'];
export type PluginCatalogView = components['schemas']['PluginCatalogView'];
export type PluginConfigEditBody = components['schemas']['PluginConfigEditBody'];
export type PluginConfigView = components['schemas']['PluginConfigView'];
export type PluginSchemaView = components['schemas']['PluginSchemaView'];
export type ReconcileBody = components['schemas']['ReconcileBody'];
export type ResearchBody = components['schemas']['ResearchBody'];
export type RetrievalBody = components['schemas']['RetrievalBody'];
export type RevisionTriple = components['schemas']['RevisionTriple'];
export type RevokeBody = components['schemas']['RevokeBody'];
export type RunCreated = components['schemas']['RunCreated'];
export type RunDetail = components['schemas']['RunDetail'];
export type RunPage = components['schemas']['RunPage'];
export type RunSubmitBody = components['schemas']['RunSubmitBody'];
export type ScheduleCreateBody = components['schemas']['ScheduleCreateBody'];
export type SchedulePage = components['schemas']['SchedulePage'];
export type ScheduleUpdateBody = components['schemas']['ScheduleUpdateBody'];
export type ScheduleView = components['schemas']['ScheduleView'];
export type SkillCatalogView = components['schemas']['SkillCatalogView'];
export type SkillPackageView = components['schemas']['SkillPackageView'];
export type MemoryDeleteBody = components['schemas']['MemoryDeleteBody'];
export type MemoryRecallBody = components['schemas']['MemoryRecallBody'];
export type MemoryReflectBody = components['schemas']['MemoryReflectBody'];
export type MemoryRetainBody = components['schemas']['MemoryRetainBody'];

/**
 * Liveness/readiness bodies. `app.py` returns plain dicts via JSONResponse,
 * so the OpenAPI declarations are `unknown`; these narrow types document the
 * actual observed bodies (backend/src/mai/api/app.py `liveness`/`readiness`).
 */
export interface HealthOk {
	status: string;
}
export interface ReadinessReport {
	status: 'ok' | 'unhealthy';
	database: string;
}

/** One failed API exchange, rendered from the C01 envelope. */
export class ApiError extends Error {
	readonly status: number;
	readonly code: string;
	readonly requestId: string;
	readonly fields: Readonly<Record<string, string>>;

	constructor(
		status: number,
		code: string,
		message: string,
		requestId = '',
		fields: Readonly<Record<string, string>> = {}
	) {
		super(`API ${status} ${code}: ${message}`);
		this.name = 'ApiError';
		this.status = status;
		this.code = code;
		this.requestId = requestId;
		this.fields = fields;
	}
}

/** Client-side misuse or transport failure (no server envelope involved). */
export class MaiClientError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'MaiClientError';
	}
}

export interface MaiApiClientOptions {
	/**
	 * Base URL prepended to `/api/v1/...` paths. Default '' (same-origin:
	 * the single TLS proxy routes `/api` to mai-api, C08 rule 3). Server-side
	 * callers (SSR load) pass the Compose-internal `http://mai-api:8000`.
	 */
	baseUrl?: string;
	/** Fetch implementation; defaults to global fetch. Inject for SSR/tests. */
	fetch?: typeof fetch;
	/** Extra headers on every request (e.g. SSR cookie forwarding). */
	headers?: Record<string, string>;
	/**
	 * CSRF token source for mutating requests. Default: the `mai_csrf`
	 * cookie (readable by first-party JS by contract). Returns null when
	 * unavailable — mutations then fail closed client-side.
	 */
	getCsrfToken?: () => string | null;
}

type MutatingMethod = 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** Reads one cookie value from `document.cookie`; browser-only, else null. */
function readCookie(name: string): string | null {
	if (typeof document === 'undefined') return null;
	for (const pair of document.cookie.split(';')) {
		const eq = pair.indexOf('=');
		if (eq === -1) continue;
		if (pair.slice(0, eq).trim() === name) {
			try {
				return decodeURIComponent(pair.slice(eq + 1).trim());
			} catch {
				return pair.slice(eq + 1).trim();
			}
		}
	}
	return null;
}

interface EnvelopeShape {
	code: string;
	message: string;
	requestId: string;
	fields: Record<string, string>;
}

/** Accepts only a well-formed C01 envelope; anything else is rejected. */
function parseEnvelope(body: unknown): EnvelopeShape | null {
	if (typeof body !== 'object' || body === null || !('error' in body)) return null;
	const error: unknown = body.error;
	if (typeof error !== 'object' || error === null) return null;
	if (!('code' in error) || !('message' in error)) return null;
	if (typeof error.code !== 'string' || typeof error.message !== 'string') return null;
	const fields: Record<string, string> = {};
	if ('fields' in error && typeof error.fields === 'object' && error.fields !== null) {
		for (const [key, value] of Object.entries(error.fields)) {
			if (typeof value === 'string') fields[key] = value;
		}
	}
	return {
		code: error.code,
		message: error.message,
		requestId: 'request_id' in error && typeof error.request_id === 'string' ? error.request_id : '',
		fields
	};
}

/**
 * Render one non-OK response as an ApiError: the C01 envelope when the body
 * parses as one, otherwise a generic status-derived fallback. Shared by the
 * request and event-stream paths so both surface identical error shapes.
 */
async function responseError(response: Response): Promise<ApiError> {
	try {
		const envelope = parseEnvelope(await response.json());
		if (envelope) {
			return new ApiError(
				response.status,
				envelope.code,
				envelope.message,
				envelope.requestId,
				envelope.fields
			);
		}
	} catch {
		// non-JSON error body: status-derived fallback below
	}
	return new ApiError(
		response.status,
		'request_rejected',
		response.status >= 500 ? 'internal server error' : 'request rejected'
	);
}

/** Frozen event envelope (C05 rule 4); `05-jobs-api` produces, UI consumes. */
export interface MaiRunEvent {
	seq: number;
	run_id: string;
	ts: string;
	type: string;
	v: number;
	data: unknown;
}

/** One parsed SSE frame: raw id/event plus decoded data text. */
export interface SseFrame {
	id: string | null;
	event: string | null;
	data: string;
}

/** Narrow a parsed SSE frame to the frozen MaiRunEvent shape, else null. */
export function readMaiEvent(frame: SseFrame): MaiRunEvent | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(frame.data);
	} catch {
		return null;
	}
	if (typeof parsed !== 'object' || parsed === null) return null;
	if (
		!('seq' in parsed) ||
		!('run_id' in parsed) ||
		!('ts' in parsed) ||
		!('type' in parsed) ||
		!('v' in parsed)
	) {
		return null;
	}
	if (
		typeof parsed.seq !== 'number' ||
		typeof parsed.run_id !== 'string' ||
		typeof parsed.ts !== 'string' ||
		typeof parsed.type !== 'string' ||
		typeof parsed.v !== 'number'
	) {
		return null;
	}
	return {
		seq: parsed.seq,
		run_id: parsed.run_id,
		ts: parsed.ts,
		type: parsed.type,
		v: parsed.v,
		data: 'data' in parsed ? parsed.data : undefined
	};
}

export interface SubscribeEventsOptions {
	/** Replay cursor to resume from; travels as the SSE `Last-Event-ID`. */
	cursor?: string | null;
	/** Abort to close the stream; the returned promise then resolves. */
	signal?: AbortSignal;
	/** Called once per complete SSE frame, in stream order. */
	onEvent: (frame: SseFrame) => void;
	/** Fetch implementation; defaults to global fetch. */
	fetch?: typeof fetch;
}

/**
 * Subscribe to an authenticated, origin-checked event stream (C08 rule 7).
 * Replay is read-only: this primitive never submits, cancels or retargets
 * anything (windowed ADR §7 "Backend independence"). Resolves when the
 * server closes the stream or `signal` aborts; rejects on HTTP/network
 * failure with `ApiError`/`TypeError` respectively.
 */
export async function subscribeEvents(
	url: string,
	options: SubscribeEventsOptions
): Promise<void> {
	const doFetch = options.fetch ?? globalThis.fetch;
	const headers: Record<string, string> = { accept: 'text/event-stream' };
	if (options.cursor) headers['last-event-id'] = options.cursor;
	const response = await doFetch(url, {
		method: 'GET',
		credentials: 'include',
		headers,
		signal: options.signal
	});
	if (!response.ok) throw await responseError(response);
	if (!response.body) {
		throw new MaiClientError('event stream closed without a body');
	}
	const reader = response.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	const emitFrame = (raw: string) => {
		const frame = parseSseFrame(raw);
		if (frame) options.onEvent(frame);
	};
	try {
		for (;;) {
			const { done, value } = await reader.read();
			if (done) break;
			buffer += decoder.decode(value, { stream: true });
			let boundary = buffer.indexOf('\n\n');
			while (boundary !== -1) {
				emitFrame(buffer.slice(0, boundary));
				buffer = buffer.slice(boundary + 2);
				boundary = buffer.indexOf('\n\n');
			}
		}
		buffer += decoder.decode();
		if (buffer.trim()) emitFrame(buffer);
	} finally {
		reader.releaseLock();
	}
}

/** Parses one SSE frame (LF or CRLF line endings; comments ignored). */
function parseSseFrame(raw: string): SseFrame | null {
	const dataLines: string[] = [];
	let id: string | null = null;
	let event: string | null = null;
	for (const line of raw.split(/\r?\n/)) {
		if (!line || line.startsWith(':')) continue;
		const colon = line.indexOf(':');
		const field = colon === -1 ? line : line.slice(0, colon);
		const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
		if (field === 'data') dataLines.push(value);
		else if (field === 'id') id = value;
		else if (field === 'event') event = value;
		// `retry` and unknown fields are ignored (transport concern only)
	}
	if (dataLines.length === 0) return null;
	return { id, event, data: dataLines.join('\n') };
}

/** Renders optional bounded-page parameters as a JOIN suffix (`''` or `&…`). */
function pageSuffix(page?: { limit?: number; offset?: number }): string {
	const params = new URLSearchParams();
	if (page?.limit !== undefined) params.set('limit', String(page.limit));
	if (page?.offset !== undefined) params.set('offset', String(page.offset));
	const query = params.toString();
	return query === '' ? '' : `&${query}`;
}

/**
 * Typed boundary over the frozen P02 surface (`operations` of the schema),
 * grown by mai-wire-desktop with the full MAI v1 feature transport (P03–P12)
 * so every feature entry's services bag is satisfied from ONE client.
 * Method names that would collide across panels are client-unique (e.g.
 * `listModelProfiles` vs the servers `listProfiles`); the per-panel slices
 * composed in DesktopShell re-map them onto each panel's exact contract.
 */
export interface MaiApiClient {
	health(): Promise<HealthOk>;
	readiness(): Promise<ReadinessReport>;
	bootstrap(payload: PasswordBody): Promise<void>;
	login(payload: PasswordBody): Promise<void>;
	logout(): Promise<void>;
	refreshCsrf(): Promise<void>;
	/** POST /api/v1/servers — the service mints `server_id` (C01 rule 1). */
	createProfile(payload: ProfileCreate): Promise<ProfileMutationResult>;
	/** GET /api/v1/servers — every stored profile, deterministic order. */
	listProfiles(): Promise<{ servers: ProfileResponse[] }>;
	/** GET /api/v1/servers/{server_id} — explicit target, never a selection. */
	readProfile(serverId: string): Promise<ProfileResponse>;
	/** PUT /api/v1/servers/{server_id} — revision-checked edit (C01 rule 6). */
	editProfile(serverId: string, payload: ProfileEdit): Promise<ProfileMutationResult>;

	/** GET /api/v1/grants — committed policy (404 before first commit). */
	readGrants(): Promise<GrantsConfigView>;
	/** PUT /api/v1/grants — one versioned edit; the route derives revision+1. */
	editGrants(payload: GrantsEditBody): Promise<GrantsMutationResult>;
	/** GET /api/v1/approvals/{approval_id} — safe preview of one decision. */
	readApproval(approvalId: string): Promise<ApprovalView>;
	/** POST /api/v1/approvals/{approval_id}/decision — one-use consumption. */
	decideApproval(approvalId: string, payload: DecisionBody): Promise<ApprovalView>;

	/** POST /api/v1/jobs/runs — durable intent; HTTP never executes (C05). */
	submitRun(payload: RunSubmitBody): Promise<RunCreated>;
	/** GET /api/v1/jobs/runs/{run_id} — replayed state + durable steps. */
	readRun(runId: string): Promise<RunDetail>;
	/** POST /api/v1/jobs/runs/{run_id}/cancel — records the request once. */
	cancelRun(runId: string, payload: CancelBody): Promise<CancelView>;
	/** GET /api/v1/jobs/runs?server_id=… — explicit scope, never unfiltered. */
	listRuns(serverId: string, page?: PageQuery): Promise<RunPage>;
	/** POST /api/v1/password-challenges — spends one decision, one secret. */
	submitChallenge(payload: ChallengeBody): Promise<ChallengeOutcomeView>;
	/** GET /api/v1/schedules?server_id=… — optional but EXPLICIT scope. */
	listSchedules(serverId: string | null, page?: PageQuery): Promise<SchedulePage>;
	/** POST /api/v1/schedules — durable configuration; target bound here. */
	createSchedule(payload: ScheduleCreateBody): Promise<ScheduleView>;
	/** PATCH /api/v1/schedules/{schedule_id} — pause/resume only. */
	updateSchedule(scheduleId: string, payload: ScheduleUpdateBody): Promise<ScheduleView>;
	/** DELETE /api/v1/schedules/{schedule_id} — row + fires cascade. */
	deleteSchedule(scheduleId: string): Promise<void>;

	/** POST /api/v1/chat/messages — durable intent; idempotent on `run_id`. */
	submitMessage(payload: ChatSubmitBody): Promise<ChatCreated>;
	/** GET /api/v1/chat/runs?server_id=… — explicitly scoped page. */
	listConversations(serverId: string, page?: PageQuery): Promise<ChatPage>;
	/** GET /api/v1/chat/runs/{run_id} — replayed conversation record. */
	readConversation(runId: string): Promise<ConversationView>;
	/** POST /api/v1/chat/runs/{run_id}/cancel — explicit tree cancellation. */
	cancelConversation(runId: string, payload: ChatCancelBody): Promise<CancelView>;

	/** GET /api/v1/models/connections — value-free references. */
	listConnections(): Promise<ModelConnectionList>;
	/** GET /api/v1/models/connections/{name} — credential reference only. */
	readConnection(name: string): Promise<ModelConnectionView>;
	/** PUT /api/v1/models/connections/{name} — revision-checked upsert. */
	putConnection(name: string, payload: ModelConnectionBody): Promise<ModelMutationResult>;
	/** DELETE /api/v1/models/connections/{name}?expected_revision=… */
	deleteConnection(name: string, expectedRevision: number): Promise<void>;
	/** GET /api/v1/models/profiles — value-free references. */
	listModelProfiles(): Promise<ModelProfileList>;
	/** GET /api/v1/models/profiles/{name} — allowlisted settings view. */
	readModelProfile(name: string): Promise<ModelProfileView>;
	/** PUT /api/v1/models/profiles/{name} — revision-checked upsert. */
	putModelProfile(name: string, payload: ModelProfileBody): Promise<ModelMutationResult>;
	/** DELETE /api/v1/models/profiles/{name}?expected_revision=… */
	deleteModelProfile(name: string, expectedRevision: number): Promise<void>;
	/** GET /api/v1/models/selection — 404 when none is stored. */
	readSelection(): Promise<ModelSelectionView>;
	/** PUT /api/v1/models/selection — validated pair, optimistic revision. */
	putSelection(payload: ModelSelectionBody): Promise<ModelSelectionView>;
	/** DELETE /api/v1/models/selection?expected_revision=… */
	deleteSelection(expectedRevision: number): Promise<void>;
	/** POST /api/v1/models/test — one bounded classified probe round. */
	probeModel(payload: ModelProbeBody): Promise<ModelProbeOutcome>;

	/** GET /api/v1/docs?server_id=… — one scope's live projection page. */
	listDocuments(serverId: string, page?: DocsPageQuery): Promise<DocumentPage>;
	/** GET /api/v1/docs/{doc_id}?server_id=… — hash-verified read + text. */
	readDocument(
		serverId: string,
		docId: string,
		namespace?: DocNamespace
	): Promise<DocumentView>;
	/** PUT /api/v1/docs/{doc_id} — revision-checked edit (C01 rule 6). */
	editDocument(docId: string, payload: DocumentEditBody): Promise<DocumentHeadView>;
	/** POST /api/v1/docs/retrieve — cited hybrid search over one scope. */
	retrieve(payload: RetrievalBody): Promise<RetrievalResult>;
	/** GET /api/v1/docs/index?server_id=… — durable outbox + projection. */
	readIndex(serverId: string): Promise<IndexStatus>;
	/** POST /api/v1/docs/index/reconcile — run one scope's outbox now. */
	reconcileIndex(payload: ReconcileBody): Promise<ReconcileReport>;
	/** POST /api/v1/docs/generate — preview/commit run-evidence document. */
	generateDocument(payload: GenerateBody): Promise<GenerateView>;
	/** POST /api/v1/docs/research — one bounded deep-research pass. */
	runResearch(payload: ResearchBody): Promise<ResearchReport>;

	/** GET /api/v1/servers/{id}/applications — one scope's adopted page. */
	listAdoptedApplications(
		serverId: string,
		page?: ApplicationsPageQuery
	): Promise<ApplicationPage>;
	/** GET /api/v1/servers/{id}/applications/discoveries — read-only snapshot. */
	discoverApplications(serverId: string): Promise<DiscoveryView>;
	/** POST /api/v1/servers/{id}/applications — explicit adoption (C09 rule 1). */
	adoptApplication(serverId: string, payload: AdoptBody): Promise<AdoptionCreated>;
	/** DELETE /api/v1/servers/{id}/applications/{app_id} — non-destructive revoke. */
	revokeApplication(
		serverId: string,
		applicationId: string,
		payload: RevokeBody
	): Promise<void>;

	/** GET /api/v1/plugins — availability + configured documents, separately. */
	pluginCatalog(): Promise<PluginCatalogView>;
	/** GET /api/v1/plugins/{plugin}/config — the stored document, refs only. */
	readPluginConfig(plugin: string): Promise<PluginConfigView>;
	/** PUT /api/v1/plugins/{plugin}/config — versioned full-document edit. */
	editPluginConfig(plugin: string, payload: PluginConfigEditBody): Promise<RevisionTriple>;
	/** DELETE /api/v1/plugins/{plugin}/config?expected_revision= */
	removePluginConfig(plugin: string, expectedRevision: number): Promise<void>;
	/** GET /api/v1/plugins/{plugin}/schema — schema-generated-form input. */
	readPluginSchema(plugin: string): Promise<PluginSchemaView>;
	/** GET /api/v1/plugins/{plugin}/bindings — explicit grant documents. */
	listPluginBindings(plugin: string): Promise<BindingView[]>;
	/** PUT /api/v1/plugins/{plugin}/bindings/{server_id} — grant explicitly. */
	editPluginBinding(
		plugin: string,
		serverId: string,
		payload: PluginBindingEditBody
	): Promise<RevisionTriple>;
	/** DELETE /api/v1/plugins/{plugin}/bindings/{server_id}?expected_revision= */
	removePluginBinding(plugin: string, serverId: string, expectedRevision: number): Promise<void>;
	/** POST /api/v1/plugins/{plugin}/discovery — one bounded MCP discovery. */
	runPluginDiscovery(plugin: string): Promise<McpDiscoveryReportView>;
	/** GET /api/v1/skills — loaded summaries + surfaced rejections. */
	skillCatalog(): Promise<SkillCatalogView>;
	/** GET /api/v1/skills/{skill} — one package + the binding report. */
	readSkill(skill: string): Promise<SkillPackageView>;

	/** GET /api/v1/monitoring/servers/{id}/observations — newest-first page. */
	listObservations(serverId: string, page?: OverviewPageQuery): Promise<ObservationPage>;
	/** GET /api/v1/monitoring/servers/{id}/findings — attention-ordered page. */
	listFindings(serverId: string, query?: OverviewFindingsQuery): Promise<FindingPage>;
	/** POST /api/v1/monitoring/servers/{id}/findings/evaluate — REAL evaluator. */
	evaluateFindings(serverId: string): Promise<EvaluateResult>;
	/** GET /api/v1/monitoring/servers/{id}/notifications — the inbox page. */
	listNotifications(serverId: string, query?: OverviewAlertsQuery): Promise<AlertPage>;
	/** POST /api/v1/monitoring/servers/{id}/notifications/{alert_id}/ack. */
	ackNotification(serverId: string, alertId: string): Promise<AckResult>;

	/** GET /api/v1/servers/{id}/memory/status — availability probe. */
	memoryStatus(serverId: string): Promise<MemoryStatusDocument>;
	/** POST /api/v1/servers/{id}/memory/recall — gated cited recall. */
	recallMemory(serverId: string, payload: MemoryRecallBody): Promise<MemoryRecallDocument>;
	/** POST /api/v1/servers/{id}/memory/retain — synchronous real receipt. */
	retainMemory(serverId: string, payload: MemoryRetainBody): Promise<MemoryRetainDocument>;
	/** POST /api/v1/servers/{id}/memory/reflect — bounded grounded answer. */
	reflectMemory(serverId: string, payload: MemoryReflectBody): Promise<MemoryReflectDocument>;
	/** GET /api/v1/servers/{id}/memory/export?scope=… — backup-grade read. */
	exportMemory(serverId: string, scope: string): Promise<MemoryExportDocument>;
	/** POST /api/v1/servers/{id}/memory/delete — explicit, receipted. */
	deleteMemory(serverId: string, payload: MemoryDeleteBody): Promise<MemoryDeleteDocument>;
	/** POST /api/v1/servers/{id}/models/local/preview — effect-free measurement. */
	previewModelInstall(
		serverId: string,
		payload: ModelLocalPreviewBody
	): Promise<ModelLocalPreviewView>;
	/** POST /api/v1/servers/{id}/models/local/{app}/install — durable intent. */
	installModelService(
		serverId: string,
		applicationId: string,
		payload: ModelLocalSubmitBody
	): Promise<RunCreated>;
	/** POST /api/v1/servers/{id}/models/local/{app}/pull — pull IS registration. */
	pullModelWeights(
		serverId: string,
		applicationId: string,
		payload: ModelLocalPullBody
	): Promise<RunCreated>;
}

/**
 * Build one self-contained API client. Create a fresh instance wherever a
 * request scope needs one (SSR load, browser session controller); instances
 * share nothing, so no state can leak across SSR requests or windows.
 */
export function createMaiApiClient(options: MaiApiClientOptions = {}): MaiApiClient {
	const baseUrl = options.baseUrl ?? '';
	const doFetch = options.fetch ?? globalThis.fetch.bind(globalThis);
	const extraHeaders = options.headers ?? {};
	const getCsrfToken = options.getCsrfToken ?? (() => readCookie(CSRF_COOKIE));

	/** One HTTP exchange: CSRF guard, credential cookies, C01 error mapping.
	 * CSRF class: session-bearing mutations REQUIRE the double-submit token
	 * (auth ADR §5); the pre-session setup routes `bootstrap`/`login` are
	 * exact-origin-protected instead (ADR §3/§8 — no cookie exists yet), so
	 * they attach the token only when one is already available. */
	async function send(
		method: 'GET' | MutatingMethod,
		path: string,
		body?: unknown,
		csrf: 'required' | 'optional' = 'required'
	): Promise<Response> {
		const headers: Record<string, string> = { accept: 'application/json', ...extraHeaders };
		if (body !== undefined) headers['content-type'] = 'application/json';
		if (method !== 'GET') {
			const token = getCsrfToken();
			if (token) headers[CSRF_HEADER] = token;
			else if (csrf === 'required') {
				throw new MaiClientError(
					`mutation to ${path} blocked: no ${CSRF_COOKIE} token available`
				);
			}
		}
		const response = await doFetch(baseUrl + path, {
			method,
			credentials: 'include',
			headers,
			body: body === undefined ? undefined : JSON.stringify(body)
		});
		if (!response.ok) throw await responseError(response);
		return response;
	}

	/** Success body boundary cast: the shape is owned by the generated schema
	 * (schema.generated.ts); no runtime re-validation happens client-side. */
	async function requestJson<Success>(
		method: 'GET' | MutatingMethod,
		path: string,
		body?: unknown,
		csrf: 'required' | 'optional' = 'required'
	): Promise<Success> {
		const response = await send(method, path, body, csrf);
		try {
			return (await response.json()) as Success;
		} catch {
			throw new MaiClientError(`${method} ${path}: success body was not valid JSON`);
		}
	}

	/** 204-style success: no body is read; unexpected statuses fail loudly. */
	async function requestVoid(
		method: 'GET' | MutatingMethod,
		path: string,
		body?: unknown,
		expectedStatus?: number,
		csrf: 'required' | 'optional' = 'required'
	): Promise<void> {
		const response = await send(method, path, body, csrf);
		if (expectedStatus !== undefined && response.status !== expectedStatus) {
			throw new MaiClientError(`${method} ${path}: unexpected status ${response.status}`);
		}
	}

	return {
		health: () => requestJson<HealthOk>('GET', '/api/v1/health'),
		readiness: () => requestJson<ReadinessReport>('GET', '/api/v1/health/ready'),
		// G1 setup-state routes: exact-origin layer, no session/CSRF yet (ADR §3).
		bootstrap: (payload) => requestVoid('POST', '/api/v1/auth/bootstrap', payload, 201, 'optional'),
		login: (payload) => requestVoid('POST', '/api/v1/auth/login', payload, 204, 'optional'),
		logout: () => requestVoid('POST', '/api/v1/auth/logout', undefined, 204),
		refreshCsrf: () => requestVoid('GET', '/api/v1/auth/csrf', undefined, 204),
		createProfile: (payload) => requestJson<ProfileMutationResult>('POST', '/api/v1/servers', payload),
		listProfiles: () => requestJson<{ servers: ProfileResponse[] }>('GET', '/api/v1/servers'),
		readProfile: (serverId) =>
			requestJson<ProfileResponse>('GET', `/api/v1/servers/${encodeURIComponent(serverId)}`),
		editProfile: (serverId, payload) =>
			requestJson<ProfileMutationResult>(
				'PUT',
				`/api/v1/servers/${encodeURIComponent(serverId)}`,
				payload
			),

		// Capability policy (P03).
		readGrants: () => requestJson<GrantsConfigView>('GET', '/api/v1/grants'),
		editGrants: (payload) => requestJson<GrantsMutationResult>('PUT', '/api/v1/grants', payload),
		readApproval: (approvalId) =>
			requestJson<ApprovalView>('GET', `/api/v1/approvals/${encodeURIComponent(approvalId)}`),
		decideApproval: (approvalId, payload) =>
			requestJson<ApprovalView>(
				'POST',
				`/api/v1/approvals/${encodeURIComponent(approvalId)}/decision`,
				payload
			),

		// Execution + jobs/schedules (P05).
		submitRun: (payload) => requestJson<RunCreated>('POST', '/api/v1/jobs/runs', payload),
		readRun: (runId) =>
			requestJson<RunDetail>('GET', `/api/v1/jobs/runs/${encodeURIComponent(runId)}`),
		cancelRun: (runId, payload) =>
			requestJson<CancelView>(
				'POST',
				`/api/v1/jobs/runs/${encodeURIComponent(runId)}/cancel`,
				payload
			),
		listRuns: (serverId, page) =>
			requestJson<RunPage>(
				'GET',
				`/api/v1/jobs/runs?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		submitChallenge: (payload) =>
			requestJson<ChallengeOutcomeView>('POST', '/api/v1/password-challenges', payload),
		listSchedules: (serverId, page) => {
			const params = new URLSearchParams();
			if (serverId !== null) params.set('server_id', serverId);
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			const query = params.toString();
			return requestJson<SchedulePage>(
				'GET',
				`/api/v1/schedules${query === '' ? '' : `?${query}`}`
			);
		},
		createSchedule: (payload) =>
			requestJson<ScheduleView>('POST', '/api/v1/schedules', payload),
		updateSchedule: (scheduleId, payload) =>
			requestJson<ScheduleView>(
				'PATCH',
				`/api/v1/schedules/${encodeURIComponent(scheduleId)}`,
				payload
			),
		deleteSchedule: (scheduleId) =>
			requestVoid('DELETE', `/api/v1/schedules/${encodeURIComponent(scheduleId)}`, undefined, 204),

		// Conversations (P06).
		submitMessage: (payload) => requestJson<ChatCreated>('POST', '/api/v1/chat/messages', payload),
		listConversations: (serverId, page) =>
			requestJson<ChatPage>(
				'GET',
				`/api/v1/chat/runs?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		readConversation: (runId) =>
			requestJson<ConversationView>('GET', `/api/v1/chat/runs/${encodeURIComponent(runId)}`),
		cancelConversation: (runId, payload) =>
			requestJson<CancelView>(
				'POST',
				`/api/v1/chat/runs/${encodeURIComponent(runId)}/cancel`,
				payload
			),

		// Model settings (P06); deletes carry `expected_revision` as a query param.
		listConnections: () => requestJson<ModelConnectionList>('GET', '/api/v1/models/connections'),
		readConnection: (name) =>
			requestJson<ModelConnectionView>(
				'GET',
				`/api/v1/models/connections/${encodeURIComponent(name)}`
			),
		putConnection: (name, payload) =>
			requestJson<ModelMutationResult>(
				'PUT',
				`/api/v1/models/connections/${encodeURIComponent(name)}`,
				payload
			),
		deleteConnection: (name, expectedRevision) =>
			requestVoid(
				'DELETE',
				`/api/v1/models/connections/${encodeURIComponent(name)}?expected_revision=${expectedRevision}`,
				undefined,
				204
			),
		listModelProfiles: () => requestJson<ModelProfileList>('GET', '/api/v1/models/profiles'),
		readModelProfile: (name) =>
			requestJson<ModelProfileView>('GET', `/api/v1/models/profiles/${encodeURIComponent(name)}`),
		putModelProfile: (name, payload) =>
			requestJson<ModelMutationResult>(
				'PUT',
				`/api/v1/models/profiles/${encodeURIComponent(name)}`,
				payload
			),
		deleteModelProfile: (name, expectedRevision) =>
			requestVoid(
				'DELETE',
				`/api/v1/models/profiles/${encodeURIComponent(name)}?expected_revision=${expectedRevision}`,
				undefined,
				204
			),
		readSelection: () => requestJson<ModelSelectionView>('GET', '/api/v1/models/selection'),
		putSelection: (payload) =>
			requestJson<ModelSelectionView>('PUT', '/api/v1/models/selection', payload),
		deleteSelection: (expectedRevision) =>
			requestVoid(
				'DELETE',
				`/api/v1/models/selection?expected_revision=${expectedRevision}`,
				undefined,
				204
			),
		probeModel: (payload) => requestJson<ModelProbeOutcome>('POST', '/api/v1/models/test', payload),

		// Documents (P07).
		listDocuments: (serverId, page) =>
			requestJson<DocumentPage>(
				'GET',
				`/api/v1/docs?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		readDocument: (serverId, docId, namespace) =>
			requestJson<DocumentView>(
				'GET',
				`/api/v1/docs/${encodeURIComponent(docId)}?server_id=${encodeURIComponent(serverId)}${namespace === undefined ? '' : `&namespace=${encodeURIComponent(namespace)}`}`
			),
		editDocument: (docId, payload) =>
			requestJson<DocumentHeadView>(
				'PUT',
				`/api/v1/docs/${encodeURIComponent(docId)}`,
				payload
			),
		retrieve: (payload) => requestJson<RetrievalResult>('POST', '/api/v1/docs/retrieve', payload),
		readIndex: (serverId) =>
			requestJson<IndexStatus>('GET', `/api/v1/docs/index?server_id=${encodeURIComponent(serverId)}`),
		reconcileIndex: (payload) =>
			requestJson<ReconcileReport>('POST', '/api/v1/docs/index/reconcile', payload),
		generateDocument: (payload) =>
			requestJson<GenerateView>('POST', '/api/v1/docs/generate', payload),
		runResearch: (payload) => requestJson<ResearchReport>('POST', '/api/v1/docs/research', payload),

		// Applications (P08).
		listAdoptedApplications: (serverId, page) => {
			const params = new URLSearchParams();
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			const qs = params.toString();
			return requestJson<ApplicationPage>(
				'GET',
				`/api/v1/servers/${encodeURIComponent(serverId)}/applications${qs === '' ? '' : `?${qs}`}`
			);
		},
		discoverApplications: (serverId) =>
			requestJson<DiscoveryView>(
				'GET',
				`/api/v1/servers/${encodeURIComponent(serverId)}/applications/discoveries`
			),
		adoptApplication: (serverId, payload) =>
			requestJson<AdoptionCreated>(
				'POST',
				`/api/v1/servers/${encodeURIComponent(serverId)}/applications`,
				payload
			),
		revokeApplication: (serverId, applicationId, payload) =>
			requestVoid(
				'DELETE',
				`/api/v1/servers/${encodeURIComponent(serverId)}/applications/${encodeURIComponent(applicationId)}`,
				payload,
				204
			),

		// Plugins + skills (P09).
		pluginCatalog: () => requestJson<PluginCatalogView>('GET', '/api/v1/plugins'),
		readPluginConfig: (plugin) =>
			requestJson<PluginConfigView>(
				'GET',
				`/api/v1/plugins/${encodeURIComponent(plugin)}/config`
			),
		editPluginConfig: (plugin, payload) =>
			requestJson<RevisionTriple>(
				'PUT',
				`/api/v1/plugins/${encodeURIComponent(plugin)}/config`,
				payload
			),
		removePluginConfig: (plugin, expectedRevision) =>
			requestVoid(
				'DELETE',
				`/api/v1/plugins/${encodeURIComponent(plugin)}/config?expected_revision=${expectedRevision}`,
				undefined,
				204
			),
		readPluginSchema: (plugin) =>
			requestJson<PluginSchemaView>(
				'GET',
				`/api/v1/plugins/${encodeURIComponent(plugin)}/schema`
			),
		listPluginBindings: (plugin) =>
			requestJson<BindingView[]>(
				'GET',
				`/api/v1/plugins/${encodeURIComponent(plugin)}/bindings`
			),
		editPluginBinding: (plugin, serverId, payload) =>
			requestJson<RevisionTriple>(
				'PUT',
				`/api/v1/plugins/${encodeURIComponent(plugin)}/bindings/${encodeURIComponent(serverId)}`,
				payload
			),
		removePluginBinding: (plugin, serverId, expectedRevision) =>
			requestVoid(
				'DELETE',
				`/api/v1/plugins/${encodeURIComponent(plugin)}/bindings/${encodeURIComponent(serverId)}?expected_revision=${expectedRevision}`,
				undefined,
				204
			),
		runPluginDiscovery: (plugin) =>
			requestJson<McpDiscoveryReportView>(
				'POST',
				`/api/v1/plugins/${encodeURIComponent(plugin)}/discovery`
			),
		skillCatalog: () => requestJson<SkillCatalogView>('GET', '/api/v1/skills'),
		readSkill: (skill) =>
			requestJson<SkillPackageView>('GET', `/api/v1/skills/${encodeURIComponent(skill)}`),

		// Monitoring (P10).
		listObservations: (serverId, page) => {
			const params = new URLSearchParams();
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			const qs = params.toString();
			return requestJson<ObservationPage>(
				'GET',
				`/api/v1/monitoring/servers/${encodeURIComponent(serverId)}/observations${qs === '' ? '' : `?${qs}`}`
			);
		},
		listFindings: (serverId, query) => {
			const params = new URLSearchParams();
			if (query?.open !== null && query?.open !== undefined) {
				params.set('open', String(query.open));
			}
			if (query?.limit !== undefined) params.set('limit', String(query.limit));
			if (query?.offset !== undefined) params.set('offset', String(query.offset));
			const qs = params.toString();
			return requestJson<FindingPage>(
				'GET',
				`/api/v1/monitoring/servers/${encodeURIComponent(serverId)}/findings${qs === '' ? '' : `?${qs}`}`
			);
		},
		evaluateFindings: (serverId) =>
			requestJson<EvaluateResult>(
				'POST',
				`/api/v1/monitoring/servers/${encodeURIComponent(serverId)}/findings/evaluate`
			),
		listNotifications: (serverId, query) => {
			const params = new URLSearchParams();
			if (query?.unacknowledged !== null && query?.unacknowledged !== undefined) {
				params.set('unacknowledged', String(query.unacknowledged));
			}
			if (query?.limit !== undefined) params.set('limit', String(query.limit));
			if (query?.offset !== undefined) params.set('offset', String(query.offset));
			const qs = params.toString();
			return requestJson<AlertPage>(
				'GET',
				`/api/v1/monitoring/servers/${encodeURIComponent(serverId)}/notifications${qs === '' ? '' : `?${qs}`}`
			);
		},
		ackNotification: (serverId, alertId) =>
			requestJson<AckResult>(
				'POST',
				`/api/v1/monitoring/servers/${encodeURIComponent(serverId)}/notifications/${encodeURIComponent(alertId)}/ack`
			),

		// Memory + local models (P11).
		memoryStatus: (serverId) =>
			requestJson<MemoryStatusDocument>(
				'GET',
				`/api/v1/servers/${encodeURIComponent(serverId)}/memory/status`
			),
		recallMemory: (serverId, payload) =>
			requestJson<MemoryRecallDocument>(
				'POST',
				`/api/v1/servers/${encodeURIComponent(serverId)}/memory/recall`,
				payload
			),
		retainMemory: (serverId, payload) =>
			requestJson<MemoryRetainDocument>(
				'POST',
				`/api/v1/servers/${encodeURIComponent(serverId)}/memory/retain`,
				payload
			),
		reflectMemory: (serverId, payload) =>
			requestJson<MemoryReflectDocument>(
				'POST',
				`/api/v1/servers/${encodeURIComponent(serverId)}/memory/reflect`,
				payload
			),
		exportMemory: (serverId, scope) =>
			requestJson<MemoryExportDocument>(
				'GET',
				`/api/v1/servers/${encodeURIComponent(serverId)}/memory/export?scope=${encodeURIComponent(scope)}`
			),
		deleteMemory: (serverId, payload) =>
			requestJson<MemoryDeleteDocument>(
				'POST',
				`/api/v1/servers/${encodeURIComponent(serverId)}/memory/delete`,
				payload
			),
		previewModelInstall: (serverId, payload) =>
			requestJson<ModelLocalPreviewView>(
				'POST',
				`/api/v1/servers/${encodeURIComponent(serverId)}/models/local/preview`,
				payload
			),
		installModelService: (serverId, applicationId, payload) =>
			requestJson<RunCreated>(
				'POST',
				`/api/v1/servers/${encodeURIComponent(serverId)}/models/local/${encodeURIComponent(applicationId)}/install`,
				payload
			),
		pullModelWeights: (serverId, applicationId, payload) =>
			requestJson<RunCreated>(
				'POST',
				`/api/v1/servers/${encodeURIComponent(serverId)}/models/local/${encodeURIComponent(applicationId)}/pull`,
				payload
			)
	};
}

/** Compile-time pin: the client surface is typed by generated operations. */
export type MaiOperationName = keyof operations;
