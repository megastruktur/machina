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

// ask_29 (D7/C3): the scoped service-knowledge surface. Every type here
// derives EXCLUSIVELY from ./schema.generated.ts (paths + components) —
// the routes hand back plain JSONResponse documents, so the success
// content type is the schema's own alias. No wire models are hand-written;
// the bounded-page vocabulary is the shared PageQuery.
type KnowledgeOperationName =
	| 'read_knowledge_api_v1_servers__server_id__applications__application_id__knowledge_get'
	| 'read_fact_history_api_v1_servers__server_id__applications__application_id__knowledge_history_get'
	| 'read_operations_api_v1_servers__server_id__applications__application_id__knowledge_operations_get'
	| 'retract_fact_api_v1_servers__server_id__applications__application_id__knowledge_facts__fact_id__retract_post'
	| 'retry_operation_api_v1_servers__server_id__applications__application_id__knowledge_operations__operation_id__retry_post';

/** Retraction CAS body — schema-derived (components.RetractBody). */
type KnowledgeRetractBody = components['schemas']['RetractBody'];
/** Operation status vocabulary — schema-derived (components.OperationStatus). */
type KnowledgeOperationStatus = NonNullable<components['schemas']['OperationStatus']>;

/**
 * The knowledge routes declare CONCRETE response models (ask_26 fix2),
 * so every knowledge response type here is a PURE schema-derived alias
 * over components — zero hand-written wire models.
 */
type KnowledgeReadResponse = components['schemas']['KnowledgeRead'];
type FactHistoryResponse = components['schemas']['FactRevisionPage'];
type KnowledgeOperationsResponse = components['schemas']['OperationPage'];
type RetractionResponse = components['schemas']['RetractionResult'];
type RetryResponse = components['schemas']['OperationReceipt'];
import type {
	CatalogPageQuery,
	RunbookCatalogPage,
	RunbookExecuteBody,
	RunbookExecutionView,
	RunbookUpsertBody,
	RunbookView
} from '../workspace/entries/13-web-runbooks.js';
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

/** Guided onboarding surface (so-01/so-02 wire models; generated schema only). */
export type ControllerKeyResponse = components['schemas']['ControllerKeyResponse'];
export type OnboardingCandidate = components['schemas']['OnboardingCandidate'];
export type OnboardingDiscoveryResponse = components['schemas']['OnboardingDiscoveryResponse'];
export type OnboardingEnrollRequest = components['schemas']['OnboardingEnrollRequest'];
export type OnboardingIdentityView = components['schemas']['OnboardingIdentityView'];
export type OnboardingSshTarget = components['schemas']['OnboardingSshTarget'];
export type OnboardingTestRequest = components['schemas']['OnboardingTestRequest'];
export type OnboardingTestResult = components['schemas']['OnboardingTestResult'];

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
export type InstalledPluginView = components['schemas']['InstalledPluginView'];
export type PluginProbeBody = components['schemas']['PluginProbeBody'];
export type PluginProbeView = components['schemas']['PluginProbeView'];
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
export type TerminalSessionInfoView = components['schemas']['TerminalSessionInfoView'];
export type MemoryDeleteBody = components['schemas']['MemoryDeleteBody'];
export type MemoryRecallBody = components['schemas']['MemoryRecallBody'];
export type MemoryReflectBody = components['schemas']['MemoryReflectBody'];
export type MemoryRetainBody = components['schemas']['MemoryRetainBody'];





/**
 * History-tag management surface (history-tags W6B). Types come verbatim
 * from the generated schema; these aliases only give the methods and their
 * consumers stable names. The catalog is global (server-agnostic); the
 * assignment pages are explicitly server-scoped per entity.
 */
export type TagView = components['schemas']['TagView'];
export type TagPage = components['schemas']['TagPage'];
export type TagCreateBody = components['schemas']['TagCreateBody'];
export type TagAssignBody = components['schemas']['TagAssignBody'];
export type EntityTagView = components['schemas']['EntityTagView'];
export type JobTagsPage = components['schemas']['JobTagsPage'];

// --- Session-first surface (D07; sfc_PLAN D07, BRIEF_D07 §2.1) -------------
// The typed /api/v1/agent-sessions vocabulary. Payload/response shapes come
// exclusively from schema.generated.ts (the D06 backend wire); the aliases
// below give the panel/session-client layer its stable names without ever
// restating field shapes.

/** One session header/detail view (generated SessionDetail). */
export type AgentSessionView = components['schemas']['SessionDetail'];
/** One bounded session list row (generated SessionSummary). */
export type AgentSessionSummary = components['schemas']['SessionSummary'];
/** One bounded session list page (generated SessionListPage envelope). */
export type AgentSessionPage = components['schemas']['SessionListPage'];
/** One durable session entry envelope (generated SessionEntryView). */
export type SessionEntryEnvelope = components['schemas']['SessionEntryView'];
/** One bounded entries/history page (generated SessionHistoryPage). */
export type SessionEntriesPage = components['schemas']['SessionHistoryPage'];
export type SessionCreateRequest = components['schemas']['SessionCreateRequest'];
export type SessionTurnSubmitRequest = components['schemas']['SessionTurnSubmitRequest'];
export type SessionTurnSubmitOutcome = components['schemas']['SessionTurnSubmitResponse'];
export type SessionControlRequest = components['schemas']['SessionControlRequest'];
export type SessionControlOutcome = components['schemas']['SessionControlResponse'];
export type SessionStatus = components['schemas']['SessionStatus'];
export type SessionRunState = components['schemas']['SessionRunState'];
/** Rename body for a session (generated; the D07-fix1 backend route). */
export type SessionRenameRequest = components['schemas']['SessionRenameRequest'];
/**
 * One entity's session-tag page (generated; the D07-fix1 backend route
 * mirrors the reviewed ConversationTagsPage envelope field-for-field).
 */
export type SessionTagsPage = components['schemas']['SessionTagsPage'];
export type SessionTagAssignRequest = components['schemas']['SessionTagAssignRequest'];

/** Bounded session-list query (AGENTS.md §1: every collection read is a page). */
export interface AgentSessionListQuery {
	/** Explicit server scope; omit/null for the unfiltered recent page. */
	server_id?: string | null;
	/** Optional coarse status filter (SessionStatus vocabulary). */
	status?: SessionStatus | null;
	/** Optional catalog tag id filter. */
	tag?: string | null;
	/** Page size; producer default and maximum are the reviewed bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

/** Bounded session entries query (cursor pages; AGENTS.md §1). */
export interface AgentSessionEntriesQuery {
	/** Explicit server scope. */
	server_id?: string | null;
	/** Older-than cursor: strictly-lower seq exclusive page. */
	before_seq?: number | null;
	/** Newer-than cursor: strictly-higher seq exclusive page (replay). */
	after_seq?: number | null;
	/** Page size; producer default 30 and maximum 50 are the reviewed bounds. */
	limit?: number;
}

/**
 * Query of the task items/constraints pages: the bounded page plus the
 * explicit P3 history switch. Superseded rows stay RECORDED server-side and
 * are served only when a caller asks for them by name.
 */
export interface TaskCollectionQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
	/** Include superseded rows (recorded P3 history) in the page. */
	includeSuperseded?: boolean;
}

/**
 * Query of the canonical events page: the `afterSeq` replay cursor (the
 * conversation-local monotonic sequence; only strictly newer events are
 * returned) plus the bounded page size. There is no `offset` on this route —
 * the cursor IS the resume position, so the filtered set is always served
 * from its start and reconnect dedup is by construction.
 */
export interface ConversationEventsQuery {
	/** Replay cursor: return only events strictly newer than this sequence. */
	afterSeq?: number;
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
}

/** Query of the global tag catalog page (HT-W6B): bounded page + filter. */
export interface TagCatalogQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
	/** Case-insensitive substring filter over tag names. */
	nameFilter?: string;
}

/** Query of one entity's tag page (HT-W6B): the bounded page only. */
export interface TagEntityTagsQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

/** Options of the catalog DELETE: the store's used/unused policy knobs. */
export interface TagDeleteOptions {
	/** Required to delete a tag that still has associations. */
	force?: boolean;
	/** Re-classification target for entities whose ONLY tag is deleted. */
	replacementTagId?: string;
}

/** Options of an association DELETE: explicit scope + optional replacement. */
export interface TagRemovalOptions {
	/** Explicit server scope (C01 rule 2). */
	serverId: string;
	/** Replacement committed in the same transaction on final-tag removal. */
	replacementTagId?: string;
}

/** Runs-list query (HT-W6B): the reviewed page + the bounded tag filter. */
export interface TaggedRunsPageQuery extends PageQuery {
	/** Repeated catalog tag ids; at most 50, matched per `tagMatch`. */
	tagIds?: string[];
	/** `any` (at least one, default) or `all` (every). */
	tagMatch?: 'any' | 'all';
}

/** Canonical conversations-list query (HT-W6B): bounded page + tag filter. */
export interface TaggedConversationsPageQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
	/** Repeated catalog tag ids; at most 50, matched per `tagMatch`. */
	tagIds?: string[];
	/** `any` (at least one, default) or `all` (every). */
	tagMatch?: 'any' | 'all';
}

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

// --- Notification inbox surface (nbi; notification-inbox.md) ----------------
// The unified inbox: global list/unread-count/ack/unack/mark-all-read, the
// wakeup-only SSE stream and ingest-key management. Every alias below comes
// verbatim from the generated schema (never a restated field shape). The
// canonical filter query mirrors the frozen surface (PLAN HTTP table /
// RESEARCH §R4-F2): `server_id` | `scope: 'global'` are MUTUALLY EXCLUSIVE
// (the client fails closed before any byte when both are set — the server
// would 422); `include_global` travels only beside an explicit `server_id`
// (the route ignores it otherwise); `unread`/`origin`/`source`/`q` compose;
// `limit`/`offset` ride the bounded-page vocabulary. The SSE stream carries
// identity-only wakeups and has NO cursor: there is no `Last-Event-ID` here
// — the caller refetches the durable store on every wakeup, on `resync`,
// and when the bounded stream self-closes (`onStreamEnd`).

/** One inbox notice row (0017 `notification_alerts`; `acked` mirrors the stamp). */
export type AlertView = components['schemas']['AlertView'];
/** Origin vocabulary of the unified inbox. */
export type AlertOrigin = components['schemas']['AlertOrigin'];
/** One bounded global inbox page (canonical filters, newest first). */
export type NotificationPage = components['schemas']['NotificationPage'];
/** The badge/drawer count: unread notices under the exact canonical filters. */
export type UnreadCountView = components['schemas']['UnreadCountView'];
/** Mark-all-read receipt: the database's own rowcount, never a guess. */
export type MarkAllReadResult = components['schemas']['MarkAllReadResult'];
/** One ingest key's metadata — NEVER `key_hash` or any plaintext material. */
export type IngestKeyView = components['schemas']['IngestKeyView'];
/** One bounded ingest-key metadata page (never key material). */
export type IngestKeyPageView = components['schemas']['IngestKeyPageView'];
/** Mint payload: label (1..64), optional server scope, optional expiry. */
export type CreateIngestKeyBody = components['schemas']['CreateIngestKeyBody'];
/**
 * The mint receipt: `key` is the plaintext carried EXACTLY ONCE by this one
 * response. It is an opaque one-time string for the caller to hand to the
 * service being wired — this client never logs, stores or re-transmits it.
 */
export type IssuedIngestKeyView = components['schemas']['IssuedIngestKeyView'];
/** Revoke receipt (`revoked: false` deliberately covers unknown/already-revoked). */
export type RevokeIngestKeyResult = components['schemas']['RevokeIngestKeyResult'];

/**
 * Canonical inbox filter surface (R4-F2), shared verbatim by the list,
 * unread-count and mark-all-read client methods. `server_id` and
 * `scope: 'global'` are mutually exclusive; `include_global` is meaningful
 * only beside `server_id` (default `true` server-side).
 */
export interface NotificationFilterQuery {
	/** Explicit server scope; mutually exclusive with `scope`. */
	server_id?: string | null;
	/** Global-only scope (`NULL`-server notices); mutually exclusive with `server_id`. */
	scope?: 'global' | null;
	/** Only with `server_id`: include global notices (server default `true`). */
	include_global?: boolean | null;
	/** `true` unread only, `false` read only, omit for all. */
	unread?: boolean | null;
	/** Notice origin filter (`finding | job_result | external`). */
	origin?: AlertOrigin | null;
	/** Exact source filter (external sender identity). */
	source?: string | null;
	/** Server-side ILIKE over title/summary/body/source. */
	q?: string | null;
}

/**
 * Bounded global inbox page query: the canonical filter surface plus the
 * page window (`limit`/`offset`; producer default 50, max 200). The shared
 * `PageQuery` mirror is runs-specific (its `q`/date/tag fields are runs-list
 * filters), so the page fields are restated here like every other
 * collection query in this module.
 */
export interface NotificationListQuery extends NotificationFilterQuery {
	/** Page size; producer default 50 and maximum 200 are the route bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

/**
 * One identity-only SSE wakeup. The OpenAPI declaration for the stream body
 * is `unknown` (SSE is not JSON), so this union is typed at this helper
 * boundary exactly as the producer emits it (notification_channel.py):
 * identity frames `{kind: "created" | "updated", alert_id, server_id}` or
 * the identity-free `{kind: "resync"}` refetch signal. No content, no cursor.
 */
export type NotificationWakeup =
	| { kind: 'created' | 'updated'; alert_id: string; server_id: string | null }
	| { kind: 'resync' };

/** Narrow one parsed SSE frame to the frozen wakeup union, else null. */
export function readNotificationWakeup(frame: SseFrame): NotificationWakeup | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(frame.data);
	} catch {
		return null;
	}
	if (typeof parsed !== 'object' || parsed === null || !('kind' in parsed)) return null;
	if (parsed.kind === 'resync') return { kind: 'resync' };
	if (parsed.kind !== 'created' && parsed.kind !== 'updated') return null;
	if (!('alert_id' in parsed) || typeof parsed.alert_id !== 'string') return null;
	if (
		!('server_id' in parsed) ||
		(parsed.server_id !== null && typeof parsed.server_id !== 'string')
	) {
		return null;
	}
	return {
		kind: parsed.kind,
		alert_id: parsed.alert_id,
		server_id: parsed.server_id as string | null
	};
}

export interface SubscribeNotificationEventsOptions {
	/** Abort to close the stream; the returned promise then resolves. */
	signal?: AbortSignal;
	/** One parsed identity/resync wakeup, in stream order. */
	onWakeup: (wakeup: NotificationWakeup) => void;
	/**
	 * Refetch/resubscribe hook: the bounded stream self-closes (and any clean
	 * server close ends the promise) — reconnect means refetch the durable
	 * store and call this binding again. Not invoked after a caller abort.
	 */
	onStreamEnd?: () => void;
	/** Fetch implementation; defaults to global fetch. */
	fetch?: typeof fetch;
}

/**
 * Subscribe to the wakeup-only inbox stream (`GET /api/v1/notifications/events`,
 * session + origin before the first byte). Wakeup-only: NO `Last-Event-ID`
 * cursor is ever sent — every frame ends in a caller refetch, so any
 * reconnect timing is safe. Resolves when the server closes its bounded
 * stream or `signal` aborts; rejects on HTTP/network failure with
 * `ApiError`/`TypeError` respectively.
 */
export async function subscribeNotificationEvents(
	url: string,
	options: SubscribeNotificationEventsOptions
): Promise<void> {
	await subscribeEvents(url, {
		fetch: options.fetch,
		signal: options.signal,
		onEvent: (frame) => {
			const wakeup = readNotificationWakeup(frame);
			if (wakeup) options.onWakeup(wakeup);
		}
	});
	if (!options.signal?.aborted) options.onStreamEnd?.();
}

/** Renders the canonical inbox filters; mutual exclusion fails closed here. */
function inboxFilterParams(query: NotificationFilterQuery): URLSearchParams {
	if (query.server_id && query.scope) {
		throw new MaiClientError(
			'notification filters rejected: server_id and scope are mutually exclusive'
		);
	}
	const params = new URLSearchParams();
	if (query.server_id) params.set('server_id', query.server_id);
	if (query.scope) params.set('scope', query.scope);
	// include_global is meaningful ONLY beside an explicit server_id (the
	// route ignores it otherwise); sending it standalone would be noise.
	if (query.server_id && query.include_global !== undefined && query.include_global !== null) {
		params.set('include_global', String(query.include_global));
	}
	if (query.unread !== undefined && query.unread !== null) {
		params.set('unread', String(query.unread));
	}
	if (query.origin) params.set('origin', query.origin);
	if (query.source) params.set('source', query.source);
	if (query.q) params.set('q', query.q);
	return params;
}

/** Renders optional bounded-page parameters as a JOIN suffix (`''` or `&…`). */
function pageSuffix(page?: { limit?: number; offset?: number }): string {
	const params = new URLSearchParams();
	if (page?.limit !== undefined) params.set('limit', String(page.limit));
	if (page?.offset !== undefined) params.set('offset', String(page.offset));
	const query = params.toString();
	return query === '' ? '' : `&${query}`;
}

/** Scoped service-knowledge route prefix (ask_26/D7): the path
 * identities are the ONLY target — never an ambient scope. */
function knowledgeBase(serverId: string, applicationId: string): string {
	return (
		`/api/v1/servers/${encodeURIComponent(serverId)}` +
		`/applications/${encodeURIComponent(applicationId)}/knowledge`
	);
}

/** Renders the task-collection query (bounded page + the P3 history switch). */
function taskQuerySuffix(query?: TaskCollectionQuery): string {
	const params = new URLSearchParams();
	if (query?.limit !== undefined) params.set('limit', String(query.limit));
	if (query?.offset !== undefined) params.set('offset', String(query.offset));
	if (query?.includeSuperseded !== undefined) {
		params.set('include_superseded', String(query.includeSuperseded));
	}
	const rendered = params.toString();
	return rendered === '' ? '' : `&${rendered}`;
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

	/**
	 * GET /api/v1/servers/onboarding/controller-key — the controller's public
	 * key half only (no resolver path, private material or env value).
	 */
	readControllerKey(): Promise<ControllerKeyResponse>;
	/**
	 * POST /api/v1/servers/onboarding/test — side-effect-free connection test:
	 * `host_key: null` is candidate discovery ONLY, a fingerprint is a pinned
	 * authentication test (`OnboardingTestResult`).
	 */
	testOnboardingConnection(
		payload: OnboardingTestRequest
	): Promise<OnboardingDiscoveryResponse | OnboardingTestResult>;
	/**
	 * POST /api/v1/servers/onboarding/enroll — confirmed guided enrollment.
	 * The browser never submits `credential_ref`; the server stores its own
	 * configured default. `host_key_confirmed: true` is explicit consent.
	 */
	enrollViaOnboarding(payload: OnboardingEnrollRequest): Promise<ProfileMutationResult>;

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
	listRuns(serverId: string, page?: TaggedRunsPageQuery): Promise<RunPage>;
	/** POST /api/v1/password-challenges — spends one decision, one secret. */
	submitChallenge(payload: ChallengeBody): Promise<ChallengeOutcomeView>;
	/** GET /api/v1/servers/{server_id}/terminal/info — terminal attach view. */
	terminalInfo(serverId: string): Promise<TerminalSessionInfoView>;
	/** GET /api/v1/schedules?server_id=… — optional but EXPLICIT scope. */
	listSchedules(serverId: string | null, page?: PageQuery): Promise<SchedulePage>;
	/** POST /api/v1/schedules — durable configuration; target bound here. */
	createSchedule(payload: ScheduleCreateBody): Promise<ScheduleView>;
	/** PATCH /api/v1/schedules/{schedule_id} — pause/resume only. */
	updateSchedule(scheduleId: string, payload: ScheduleUpdateBody): Promise<ScheduleView>;
	/** DELETE /api/v1/schedules/{schedule_id} — row + fires cascade. */
	deleteSchedule(scheduleId: string): Promise<void>;

	// History-tag management surface (W6B). The catalog is global; entity
	// reads/mutations take their explicit `server_id` (C01 rule 2), and the
	// zero-UUID General Consult scope is valid on the conversation routes
	// ONLY. Assignments are idempotent 204s; the deletion routes speak the
	// store's used/unused policy (force + same-transaction replacement).
	/** GET /api/v1/tags — bounded catalog page with the name filter. */
	listTags(query?: TagCatalogQuery): Promise<TagPage>;
	/** POST /api/v1/tags — idempotent canonical create (same row back). */
	createTag(payload: TagCreateBody): Promise<TagView>;
	/** DELETE /api/v1/tags/{tag_id} — used tags need force (+ replacement). */
	deleteTag(tagId: string, options?: TagDeleteOptions): Promise<void>;
	/** GET /api/v1/jobs/runs/{run_id}/tags?server_id=… — bounded run page. */
	listRunTags(
		runId: string,
		serverId: string,
		page?: TagEntityTagsQuery
	): Promise<JobTagsPage>;
	/** POST /api/v1/jobs/runs/{run_id}/tags — idempotent user assignment. */
	assignRunTag(runId: string, payload: TagAssignBody): Promise<void>;
	/** DELETE /api/v1/jobs/runs/{run_id}/tags/{tag_id}?server_id=… — removal. */
	removeRunTag(runId: string, tagId: string, options: TagRemovalOptions): Promise<void>;
	// --- Session-first surface (D07; sfc_PLAN D07) ---------------------------
	// The typed /api/v1/agent-sessions transport. Same boundary invariants as
	// every other slice: C01 envelopes fail through `ApiError`, the session
	// cookie is the only credential, mutations carry the CSRF double submit,
	// every collection read is an explicitly-scoped bounded page (AGENTS.md
	// §1), and there is no client-level "current session/server" anywhere.
	/**
	 * POST /api/v1/agent-sessions — idempotent session create (the client
	 * idempotency key derives the session identity; a retry converges on the
	 * same row).
	 */
	createAgentSession(payload: SessionCreateRequest): Promise<AgentSessionView>;
	/**
	 * GET /api/v1/agent-sessions — bounded deterministic page
	 * (`updated_at DESC, session_id`) with the honest total/has_more.
	 */
	listAgentSessions(query?: AgentSessionListQuery): Promise<AgentSessionPage>;
	/** GET /api/v1/agent-sessions/{id}?server_id=… — safe header detail. */
	readAgentSession(sessionId: string, serverId?: string | null): Promise<AgentSessionView>;
	/** PATCH /api/v1/agent-sessions/{id} — durable user-assigned title. */
	renameAgentSession(sessionId: string, payload: SessionRenameRequest): Promise<AgentSessionView>;
	/**
	 * GET /api/v1/agent-sessions/{id}/history?server_id=… — bounded
	 * cursor-exclusive entries page (`before_seq` older / `after_seq` newer
	 * replay; producer default 30, max 50).
	 */
	listAgentSessionEntries(
		sessionId: string,
		query?: AgentSessionEntriesQuery
	): Promise<SessionEntriesPage>;
	/**
	 * POST /api/v1/agent-sessions/{id}/turns — durable intent admission
	 * (idempotent on `client_request_id`); the worker owns execution.
	 */
	submitAgentSessionTurn(
		sessionId: string,
		payload: SessionTurnSubmitRequest,
		serverId?: string | null
	): Promise<SessionTurnSubmitOutcome>;
	/**
	 * POST /api/v1/agent-sessions/{id}/control — steer/pause/cancel/
	 * approve/deny/reconcile through the reviewed session-runner facade.
	 */
	submitAgentSessionControl(
		sessionId: string,
		payload: SessionControlRequest,
		serverId?: string | null
	): Promise<SessionControlOutcome>;
	/** GET /api/v1/agent-sessions/{id}/tags?server_id=… — bounded tag page. */
	listAgentSessionTags(
		sessionId: string,
		serverId: string,
		page?: TagEntityTagsQuery
	): Promise<SessionTagsPage>;
	/** POST /api/v1/agent-sessions/{id}/tags — idempotent user assignment. */
	assignAgentSessionTag(sessionId: string, payload: SessionTagAssignRequest): Promise<void>;
	/** DELETE /api/v1/agent-sessions/{id}/tags/{tag_id}?server_id=… — removal. */
	removeAgentSessionTag(
		sessionId: string,
		tagId: string,
		options: TagRemovalOptions
	): Promise<void>;

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
	/** POST /api/v1/servers/{id}/applications/{app_id}/execute — background runbook job. */
	executeRunbook(
		serverId: string,
		applicationId: string,
		payload: RunbookExecuteBody
	): Promise<RunbookExecutionView>;

	/** GET /api/v1/applications/catalog — one bounded global catalog page. */
	listCatalogRunbooks(page?: CatalogPageQuery): Promise<RunbookCatalogPage>;
	/** POST /api/v1/applications/catalog — upsert one global runbook. */
	upsertCatalogRunbook(payload: RunbookUpsertBody): Promise<RunbookView>;
	/** DELETE /api/v1/applications/catalog/{runbook_id} — delete the recipe only. */
	deleteCatalogRunbook(runbookId: string): Promise<void>;

	/** GET /api/v1/plugins — availability + configured documents, separately. */
	pluginCatalog(page?: PageQuery): Promise<PluginCatalogView>;
	/** POST /api/v1/plugins/{plugin}/probe — value-free readiness check. */
	probePlugin(plugin: string, body?: PluginProbeBody): Promise<PluginProbeView>;
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
	/** POST /api/v1/monitoring/servers/{id}/notifications/{alert_id}/unacknowledge. */
	unacknowledgeServerNotification(serverId: string, alertId: string): Promise<AckResult>;
	// Scoped service-knowledge surface (ask_29, D7/C3): reads are
	// paginated collection methods; the operator writes are the exact
	// CAS retraction and the explicit authorized retry. Types derive
	// exclusively from ./schema.generated.ts.
	/** GET …/knowledge — bounded current knowledge read (paginated). */
	readKnowledge(
		serverId: string,
		applicationId: string,
		page?: PageQuery
	): Promise<KnowledgeReadResponse>;
	/** GET …/knowledge/history — paginated fact revision history. */
	readFactHistory(
		serverId: string,
		applicationId: string,
		factId?: string,
		page?: PageQuery
	): Promise<FactHistoryResponse>;
	/** GET …/knowledge/operations — paginated preservation receipts. */
	readKnowledgeOperations(
		serverId: string,
		applicationId: string,
		status?: KnowledgeOperationStatus,
		page?: PageQuery
	): Promise<KnowledgeOperationsResponse>;
	/** POST …/knowledge/facts/{fact_id}/retract — operator CAS retraction
	 * (session + allowed-origin + CSRF; stale CAS answers 409). */
	retractKnowledgeFact(
		serverId: string,
		applicationId: string,
		factId: string,
		payload: KnowledgeRetractBody
	): Promise<RetractionResponse>;
	/** POST …/knowledge/operations/{operation_id}/retry — explicit
	 * authorized retry of an unacknowledged preservation operation. */
	retryKnowledgeOperation(
		serverId: string,
		applicationId: string,
		operationId: string
	): Promise<RetryResponse>;

	// Notification inbox surface (nbi). The global surface is fleet-wide with
	// EXPLICIT canonical filters (never an ambient scope); the per-server
	// routes keep their explicit `server_id` argument (C01 rule 2). Session
	// mutations ride the CSRF double submit; the ingest-key plaintext is an
	// opaque one-time create receipt — never logged or persisted here.
	/** GET /api/v1/notifications — bounded unified-inbox page under the canonical filters. */
	listInboxNotifications(query?: NotificationListQuery): Promise<NotificationPage>;
	/** GET /api/v1/notifications/unread-count — unread total under the same filters. */
	getUnreadNotificationCount(query?: NotificationFilterQuery): Promise<UnreadCountView>;
	/** POST /api/v1/notifications/{alert_id}/ack — idempotent, scope-blind 404. */
	acknowledgeInboxNotification(alertId: string): Promise<AckResult>;
	/** POST /api/v1/notifications/{alert_id}/unack — idempotent, re-arms reminders. */
	unacknowledgeInboxNotification(alertId: string): Promise<AckResult>;
	/** POST /api/v1/notifications/mark-all-read — ONE update under the same filters; rowcount receipt. */
	markAllNotificationsRead(query?: NotificationFilterQuery): Promise<MarkAllReadResult>;
	/**
	 * GET /api/v1/notifications/events — wakeup-only SSE binding of this
	 * client's base URL. Identity/resync frames reach `onWakeup`; the bounded
	 * self-close fires `onStreamEnd` (refetch + resubscribe there). No cursor.
	 */
	subscribeNotificationEvents(options: SubscribeNotificationEventsOptions): Promise<void>;
	/** GET /api/v1/ingest-keys — bounded metadata page (never key material). */
	listIngestKeys(page?: PageQuery): Promise<IngestKeyPageView>;
	/** POST /api/v1/ingest-keys — mints; plaintext arrives ONCE as `key`. */
	createIngestKey(payload: CreateIngestKeyBody): Promise<IssuedIngestKeyView>;
	/** POST /api/v1/ingest-keys/{key_id}/revoke — effective on the next ingest. */
	revokeIngestKey(keyId: string): Promise<RevokeIngestKeyResult>;

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
		readControllerKey: () =>
			requestJson<ControllerKeyResponse>(
				'GET',
				'/api/v1/servers/onboarding/controller-key'
			),
		testOnboardingConnection: (payload) =>
			requestJson<OnboardingDiscoveryResponse | OnboardingTestResult>(
				'POST',
				'/api/v1/servers/onboarding/test',
				payload
			),
		enrollViaOnboarding: (payload) =>
			requestJson<ProfileMutationResult>(
				'POST',
				'/api/v1/servers/onboarding/enroll',
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
		// Runs list (W1 filters + the W6B bounded tag filter ride the
		// reviewed endpoint; URLSearchParams keeps the encoding of q, the
		// explicit ISO date bounds and the repeated tag_id list honest).
		listRuns: (serverId, page) => {
			const params = new URLSearchParams({ server_id: serverId });
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			if (page?.q !== undefined && page.q !== '') params.set('q', page.q);
			if (page?.date_from !== undefined && page.date_from !== '')
				params.set('date_from', page.date_from);
			if (page?.date_to !== undefined && page.date_to !== '')
				params.set('date_to', page.date_to);
			for (const tagId of page?.tagIds ?? []) params.append('tag_id', tagId);
			if (page?.tagMatch !== undefined) params.set('tag_match', page.tagMatch);
			return requestJson<RunPage>('GET', `/api/v1/jobs/runs?${params.toString()}`);
		},
		submitChallenge: (payload) =>
			requestJson<ChallengeOutcomeView>('POST', '/api/v1/password-challenges', payload),
		terminalInfo: (serverId) =>
			requestJson<TerminalSessionInfoView>(
				'GET',
				`/api/v1/servers/${encodeURIComponent(serverId)}/terminal/info`
			),
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

		// History-tag management surface (W6B). Same transport as every
		// other method: C01 envelope errors through `ApiError`, session
		// cookies + CSRF double-submit on mutations.
		listTags: (query) => {
			const params = new URLSearchParams();
			if (query?.limit !== undefined) params.set('limit', String(query.limit));
			if (query?.offset !== undefined) params.set('offset', String(query.offset));
			if (query?.nameFilter !== undefined && query.nameFilter !== '')
				params.set('name_filter', query.nameFilter);
			const rendered = params.toString();
			return requestJson<TagPage>(
				'GET',
				`/api/v1/tags${rendered === '' ? '' : `?${rendered}`}`
			);
		},
		createTag: (payload) => requestJson<TagView>('POST', '/api/v1/tags', payload),
		deleteTag: (tagId, options) => {
			const params = new URLSearchParams();
			if (options?.force !== undefined) params.set('force', String(options.force));
			if (options?.replacementTagId !== undefined && options.replacementTagId !== '')
				params.set('replacement_tag_id', options.replacementTagId);
			const rendered = params.toString();
			return requestVoid(
				'DELETE',
				`/api/v1/tags/${encodeURIComponent(tagId)}${rendered === '' ? '' : `?${rendered}`}`,
				undefined,
				204
			);
		},
		listRunTags: (runId, serverId, page) =>
			requestJson<JobTagsPage>(
				'GET',
				`/api/v1/jobs/runs/${encodeURIComponent(runId)}/tags?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		assignRunTag: (runId, payload) =>
			requestVoid(
				'POST',
				`/api/v1/jobs/runs/${encodeURIComponent(runId)}/tags`,
				payload,
				204
			),
		removeRunTag: (runId, tagId, options) => {
			const params = new URLSearchParams({ server_id: options.serverId });
			if (options.replacementTagId !== undefined && options.replacementTagId !== '')
				params.set('replacement_tag_id', options.replacementTagId);
			return requestVoid(
				'DELETE',
				`/api/v1/jobs/runs/${encodeURIComponent(runId)}/tags/${encodeURIComponent(tagId)}?${params.toString()}`,
				undefined,
				204
			);
		},
		// Session-first surface (D07). The D06 backend routes back every
		// read/mutation below except the rename/tags trio, whose producer
		// routes land with the session-API round that owns route additions —
		// the methods exist here so the session-first composition is complete
		// and its specs pin the wire contract now (BRIEF_D07 §2.1).
		createAgentSession: (payload) =>
			requestJson<AgentSessionView>('POST', '/api/v1/agent-sessions', payload),
		listAgentSessions: (query) => {
			const params = new URLSearchParams();
			if (query?.server_id !== undefined && query.server_id !== null)
				params.set('server_id', query.server_id);
			if (query?.status !== undefined && query.status !== null)
				params.set('status', query.status);
			if (query?.tag !== undefined && query.tag !== null && query.tag !== '')
				params.set('tag', query.tag);
			if (query?.limit !== undefined) params.set('limit', String(query.limit));
			if (query?.offset !== undefined) params.set('offset', String(query.offset));
			const rendered = params.toString();
			return requestJson<AgentSessionPage>(
				'GET',
				`/api/v1/agent-sessions${rendered === '' ? '' : `?${rendered}`}`
			);
		},
		readAgentSession: (sessionId, serverId) => {
			const params = new URLSearchParams();
			if (serverId !== undefined && serverId !== null) params.set('server_id', serverId);
			const rendered = params.toString();
			return requestJson<AgentSessionView>(
				'GET',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}${rendered === '' ? '' : `?${rendered}`}`
			);
		},
		renameAgentSession: (sessionId, payload) =>
			requestJson<AgentSessionView>(
				'PATCH',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}`,
				payload
			),
		listAgentSessionEntries: (sessionId, query) => {
			const params = new URLSearchParams();
			if (query?.server_id !== undefined && query.server_id !== null)
				params.set('server_id', query.server_id);
			if (query?.before_seq !== undefined && query.before_seq !== null)
				params.set('before_seq', String(query.before_seq));
			if (query?.after_seq !== undefined && query.after_seq !== null)
				params.set('after_seq', String(query.after_seq));
			if (query?.limit !== undefined) params.set('limit', String(query.limit));
			const rendered = params.toString();
			return requestJson<SessionEntriesPage>(
				'GET',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}/history${rendered === '' ? '' : `?${rendered}`}`
			);
		},
		submitAgentSessionTurn: (sessionId, payload, serverId) => {
			const params = new URLSearchParams();
			if (serverId !== undefined && serverId !== null)
				params.set('server_id', serverId);
			const rendered = params.toString();
			return requestJson<SessionTurnSubmitOutcome>(
				'POST',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}/turns${rendered === '' ? '' : `?${rendered}`}`,
				payload
			);
		},
		submitAgentSessionControl: (sessionId, payload, serverId) => {
			const params = new URLSearchParams();
			if (serverId !== undefined && serverId !== null)
				params.set('server_id', serverId);
			const rendered = params.toString();
			return requestJson<SessionControlOutcome>(
				'POST',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}/control${rendered === '' ? '' : `?${rendered}`}`,
				payload
			);
		},
		listAgentSessionTags: (sessionId, serverId, page) =>
			requestJson<SessionTagsPage>(
				'GET',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}/tags?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		assignAgentSessionTag: (sessionId, payload) =>
			requestVoid(
				'POST',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}/tags`,
				payload,
				204
			),
		removeAgentSessionTag: (sessionId, tagId, options) => {
			const params = new URLSearchParams({ server_id: options.serverId });
			if (options.replacementTagId !== undefined && options.replacementTagId !== '')
				params.set('replacement_tag_id', options.replacementTagId);
			return requestVoid(
				'DELETE',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}/tags/${encodeURIComponent(tagId)}?${params.toString()}`,
				undefined,
				204
			);
		},
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
		executeRunbook: (serverId, applicationId, payload) =>
			requestJson<RunbookExecutionView>(
				'POST',
				`/api/v1/servers/${encodeURIComponent(serverId)}/applications/${encodeURIComponent(applicationId)}/execute`,
				payload
			),

		// Global Runbook Catalog (uir-u4).
		listCatalogRunbooks: (page) => {
			const params = new URLSearchParams();
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			const qs = params.toString();
			return requestJson<RunbookCatalogPage>(
				'GET',
				`/api/v1/applications/catalog${qs === '' ? '' : `?${qs}`}`
			);
		},
		upsertCatalogRunbook: (payload) =>
			requestJson<RunbookView>('POST', '/api/v1/applications/catalog', payload),
		deleteCatalogRunbook: (runbookId) =>
			requestVoid(
				'DELETE',
				`/api/v1/applications/catalog/${encodeURIComponent(runbookId)}`,
				undefined,
				204
			),

		// Plugins + skills (P09).
		pluginCatalog: (page) => {
			const params = new URLSearchParams();
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			const qs = params.toString();
			return requestJson<PluginCatalogView>(
				'GET',
				`/api/v1/plugins${qs === '' ? '' : `?${qs}`}`
			);
		},
		probePlugin: (plugin, body) =>
			requestJson<PluginProbeView>(
				'POST',
				`/api/v1/plugins/${encodeURIComponent(plugin)}/probe`,
				body
			),
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
		// Scoped service-knowledge surface (ask_29, D7/C3). Strict same-
		// server scope: the path identities are the ONLY target; the
		// collection reads are paginated; the writes use the session
		// CSRF transport (required token) — no secrets flow here.
		readKnowledge: (serverId, applicationId, page) => {
			const params = new URLSearchParams();
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			const qs = params.toString();
			return requestJson<KnowledgeReadResponse>(
				'GET',
				`${knowledgeBase(serverId, applicationId)}${qs === '' ? '' : `?${qs}`}`
			);
		},
		readFactHistory: (serverId, applicationId, factId, page) => {
			const params = new URLSearchParams();
			if (factId !== undefined) params.set('fact_id', factId);
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			const qs = params.toString();
			return requestJson<FactHistoryResponse>(
				'GET',
				`${knowledgeBase(serverId, applicationId)}/history${qs === '' ? '' : `?${qs}`}`
			);
		},
		readKnowledgeOperations: (serverId, applicationId, status, page) => {
			const params = new URLSearchParams();
			if (status !== undefined) params.set('status', status);
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			const qs = params.toString();
			return requestJson<KnowledgeOperationsResponse>(
				'GET',
				`${knowledgeBase(serverId, applicationId)}/operations${qs === '' ? '' : `?${qs}`}`
			);
		},
		retractKnowledgeFact: (serverId, applicationId, factId, payload) =>
			requestJson<RetractionResponse>(
				'POST',
				`${knowledgeBase(serverId, applicationId)}/facts/${encodeURIComponent(factId)}/retract`,
				payload
			),
		retryKnowledgeOperation: (serverId, applicationId, operationId) =>
			requestJson<RetryResponse>(
				'POST',
				`${knowledgeBase(serverId, applicationId)}/operations/${encodeURIComponent(operationId)}/retry`
			),
		unacknowledgeServerNotification: (serverId, alertId) =>
			requestJson<AckResult>(
				'POST',
				`/api/v1/monitoring/servers/${encodeURIComponent(serverId)}/notifications/${encodeURIComponent(alertId)}/unacknowledge`
			),

		// Notification inbox surface (nbi). Same transport as every other
		// method: C01 envelope errors collapse through `ApiError`, the session
		// cookie is the only credential, mutations carry the CSRF double
		// submit, and the canonical filters are validated client-side before
		// any byte (mutual exclusion fails closed in `inboxFilterParams`).
		listInboxNotifications: async (query) => {
			const params = inboxFilterParams(query ?? {});
			if (query?.limit !== undefined) params.set('limit', String(query.limit));
			if (query?.offset !== undefined) params.set('offset', String(query.offset));
			const rendered = params.toString();
			return requestJson<NotificationPage>(
				'GET',
				`/api/v1/notifications${rendered === '' ? '' : `?${rendered}`}`
			);
		},
		getUnreadNotificationCount: async (query) => {
			const rendered = inboxFilterParams(query ?? {}).toString();
			return requestJson<UnreadCountView>(
				'GET',
				`/api/v1/notifications/unread-count${rendered === '' ? '' : `?${rendered}`}`
			);
		},
		acknowledgeInboxNotification: (alertId) =>
			requestJson<AckResult>(
				'POST',
				`/api/v1/notifications/${encodeURIComponent(alertId)}/ack`
			),
		unacknowledgeInboxNotification: (alertId) =>
			requestJson<AckResult>(
				'POST',
				`/api/v1/notifications/${encodeURIComponent(alertId)}/unack`
			),
		markAllNotificationsRead: async (query) => {
			const rendered = inboxFilterParams(query ?? {}).toString();
			return requestJson<MarkAllReadResult>(
				'POST',
				`/api/v1/notifications/mark-all-read${rendered === '' ? '' : `?${rendered}`}`
			);
		},
		subscribeNotificationEvents: (options) =>
			subscribeNotificationEvents(`${baseUrl}/api/v1/notifications/events`, {
				...options,
				fetch: options.fetch ?? doFetch
			}),
		listIngestKeys: (page) => {
			const params = new URLSearchParams();
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			const qs = params.toString();
			return requestJson<IngestKeyPageView>(
				'GET',
				`/api/v1/ingest-keys${qs === '' ? '' : `?${qs}`}`
			);
		},
		// The mint receipt's `key` plaintext flows through as an opaque value;
		// this client never logs, stores or re-transmits it (C03 rule 6).
		createIngestKey: (payload) =>
			requestJson<IssuedIngestKeyView>('POST', '/api/v1/ingest-keys', payload),
		revokeIngestKey: (keyId) =>
			requestJson<RevokeIngestKeyResult>(
				'POST',
				`/api/v1/ingest-keys/${encodeURIComponent(keyId)}/revoke`
			),

		// Memory (P11).
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
			)
	};
}

/** Compile-time pin: the client surface is typed by generated operations. */
export type MaiOperationName = keyof operations;
