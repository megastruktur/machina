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
export type ChatCancelBody = components['schemas']['ChatCancelBody'];
export type ChatCreated = components['schemas']['ChatCreated'];
export type ChatPage = components['schemas']['ChatPage'];
export type ChatRenameBody = components['schemas']['ChatRenameBody'];
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
 * Canonical conversation surface (§3; 09-client-continuity). Types come
 * verbatim from the generated schema (`operations`/`components`); these
 * aliases only give the methods and their consumers stable names. The
 * list/detail/messages/events reads are the scoped paginated page family
 * (C01 rule 2: explicit `server_id`, never a hidden selection).
 */
export type CanonicalConversationSummary = components['schemas']['ConversationSummary'];
export type CanonicalConversationPage = components['schemas']['ConversationListPage'];
export type CanonicalConversationDetail = components['schemas']['ConversationDetailView'];
export type CanonicalTranscriptMessage = components['schemas']['TranscriptMessage'];
export type CanonicalTranscriptPage = components['schemas']['ConversationTranscriptPage'];
export type CanonicalConversationEvent = components['schemas']['ConversationEvent'];
export type CanonicalEventsPage = components['schemas']['ConversationEventsPage'];
export type CanonicalConversationTurn = components['schemas']['ConversationTurnSubmitRequest'];
export type CanonicalTurnSubmitted = components['schemas']['ConversationTurnSubmitResponse'];
export type TurnDisposition = components['schemas']['TurnDisposition'];
export type ConversationSteerRequest = components['schemas']['ConversationSteerRequest'];
export type ConversationSteerView = components['schemas']['ChatSteerView'];

/**
 * Runtime v2 objective surface (§11; activity-ui). Types come verbatim
 * from the generated schema (`components`); these aliases only give the
 * methods and their consumers stable names. Every read is explicitly
 * server-scoped and every collection read is a bounded page envelope
 * (C01 rule 2; AGENTS.md §1; runtime-v2.md §5.2).
 */
export type ObjectiveDetail = components['schemas']['ObjectiveDetail'];
export type ObjectiveSummary = components['schemas']['ObjectiveSummary'];
export type ObjectiveListPage = components['schemas']['ObjectiveListPage'];
export type ObjectiveStatus = components['schemas']['ObjectiveStatus'];
export type ObjectiveEvent = components['schemas']['ObjectiveEvent'];
export type ObjectiveEventPage = components['schemas']['ObjectiveEventPage'];
export type ObjectiveRunLink = components['schemas']['ObjectiveRunLink'];
export type ObjectiveRunPage = components['schemas']['ObjectiveRunPage'];
export type WorkJournal = components['schemas']['WorkJournal'];
export type JournalActionApplied = components['schemas']['JournalActionApplied'];
export type JournalReceiptRecorded = components['schemas']['JournalReceiptRecorded'];
export type JournalUnknownNoted = components['schemas']['JournalUnknownNoted'];
export type JournalChildLinked = components['schemas']['JournalChildLinked'];
export type JournalVerificationRecorded = components['schemas']['JournalVerificationRecorded'];
export type DrainStatusView = components['schemas']['DrainStatusView'];
export type ConversationResumeBody = components['schemas']['ConversationResumeBody'];
export type ConversationReconcileBody = components['schemas']['ConversationReconcileBody'];
export type ConversationReconcileView = components['schemas']['ConversationReconcileView'];
export type StepClassificationBody = components['schemas']['StepClassificationBody'];
export type StepResolutionView = components['schemas']['StepResolutionView'];

/** Query of the objective-ledger page: bounded page + the replay cursor. */
export interface ObjectiveEventsQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset within the ledger. */
	offset?: number;
	/** `objective.*` type filter (inside the family only). */
	type?: string;
	/** Replay cursor: strictly newer rows only (dedup by construction). */
	afterSeq?: number;
}

/**
 * Task surface (§8; 15-task-api). Types come verbatim from the generated
 * schema (`operations`/`components`); these aliases only give the methods
 * and their consumers stable names. The header read is the compact §8 view
 * (no unbounded arrays); the items/constraints/evidence/revisions reads are
 * the scoped paginated page family (C01 rule 2: explicit `server_id`, never
 * a hidden selection; AGENTS.md §1 bounded pages with same-filter totals).
 */
export type TaskView = components['schemas']['TaskView'];
export type TaskCounts = components['schemas']['TaskCounts'];
export type TaskOutcome = components['schemas']['TaskOutcome'];
export type TaskStepsPage = components['schemas']['TaskStepsPage'];
export type TaskStepView = components['schemas']['TaskStepView'];
export type TaskConstraintsPage = components['schemas']['TaskConstraintsPage'];
export type TaskConstraintView = components['schemas']['TaskConstraintView'];
export type TaskEvidencePage = components['schemas']['TaskEvidencePage'];
export type TaskEvidenceView = components['schemas']['TaskEvidenceView'];
export type TaskRevisionsPage = components['schemas']['TaskRevisionsPage'];
export type TaskRevisionView = components['schemas']['TaskRevisionView'];
export type TaskMutationOutcome = components['schemas']['TaskMutationOutcome'];
export type TaskCorrectionBody = components['schemas']['TaskCorrectionBody'];
export type TaskDecisionBody = components['schemas']['TaskDecisionBody'];
export type TaskPlanPatch = components['schemas']['TaskPlanPatch'];
export type TaskPlanStep = components['schemas']['TaskPlanStep'];
export type TaskStepOperation = components['schemas']['TaskStepOperation'];
export type ConstraintProposal = components['schemas']['ConstraintProposal'];

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
export type ConversationTagsPage = components['schemas']['ConversationTagsPage'];
export type EvidenceRef = components['schemas']['EvidenceRef'];

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
/**
 * Rename body for a session (mirrors the generated conversation
 * `ChatRenameBody` field-for-field: one bounded user-assigned title; the
 * PATCH /api/v1/agent-sessions/{session_id} producer lands with the
 * session API round that owns route additions).
 */
export interface SessionRenameRequest {
	/** User-assigned session title (1..240 chars, producer-validated). */
	title: string;
}
/**
 * One entity's session-tag page. The D06 backend's sibling tag pages
 * (JobTagsPage/ConversationTagsPage) all project the frozen TagPage
 * envelope; the agent-session producer uses the same shape.
 */
export type SessionTagsPage = components['schemas']['TagPage'];

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

/** Frozen `objective.*` ledger frame (§5; envelope at objective scope). */
export interface ObjectiveEventFrame {
	event_id: string;
	objective_id: string;
	sequence: number;
	ts: string;
	type: string;
	actor_kind: string;
	v: number;
	data: Record<string, unknown>;
	run_id: string | null;
}

/** Narrow a parsed SSE frame to the objective-ledger envelope, else null. */
export function readObjectiveEvent(frame: SseFrame): ObjectiveEventFrame | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(frame.data);
	} catch {
		return null;
	}
	if (typeof parsed !== 'object' || parsed === null) return null;
	const record = parsed as Record<string, unknown>;
	if (
		typeof record.event_id !== 'string' ||
		typeof record.objective_id !== 'string' ||
		typeof record.sequence !== 'number' ||
		typeof record.ts !== 'string' ||
		typeof record.type !== 'string' ||
		typeof record.actor_kind !== 'string' ||
		typeof record.v !== 'number' ||
		typeof record.data !== 'object' ||
		record.data === null
	) {
		return null;
	}
	return {
		event_id: record.event_id,
		objective_id: record.objective_id,
		sequence: record.sequence,
		ts: record.ts,
		type: record.type,
		actor_kind: record.actor_kind,
		v: record.v,
		data: record.data as Record<string, unknown>,
		run_id: typeof record.run_id === 'string' ? record.run_id : null
	};
}

/**
 * The objective activity-ledger SSE URL (§11; C08 rule 7: transport = SSE,
 * replay cursor = `Last-Event-ID`). The panel composes the concrete URL
 * through this one builder — never a hand-typed path.
 */
export function objectiveEventsUrl(objectiveId: string, serverId: string): string {
	return (
		`/api/v1/objectives/${encodeURIComponent(objectiveId)}/events/stream` +
		`?server_id=${encodeURIComponent(serverId)}`
	);
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

	/** POST /api/v1/chat/messages — durable intent; idempotent on `run_id`. */
	submitMessage(payload: ChatSubmitBody): Promise<ChatCreated>;
	/**
	 * GET /api/v1/chat/runs — explicitly scoped page when `serverId` is
	 * passed (identical to the frozen behaviour); with `serverId` omitted
	 * or `null` the page lists recent ROOT conversations across ALL
	 * servers + General Consult (the Floating Chat History Selector).
	 */
	listConversations(serverId?: string | null, page?: PageQuery): Promise<ChatPage>;
	/** GET /api/v1/chat/runs/{run_id} — replayed conversation record. */
	readConversation(runId: string): Promise<ConversationView>;
	/** PATCH /api/v1/chat/runs/{run_id} — rename a conversation. */
	renameConversation(runId: string, payload: ChatRenameBody): Promise<ConversationView>;
	/** POST /api/v1/chat/runs/{run_id}/cancel — explicit tree cancellation. */
	cancelConversation(runId: string, payload: ChatCancelBody): Promise<CancelView>;

	// Canonical conversation surface (§3; 09-client-continuity). Every method
	// takes its explicit `server_id`; collection reads take `page?: PageQuery`
	// (the events page takes its `afterSeq` cursor instead of an offset).
	/**
	 * GET /api/v1/chat/conversations?server_id=… — one bounded page of safe
	 * canonical conversation metadata (deterministic `updated_at DESC,
	 * conversation_id`; no transcript content ever rides a list page).
	 */
	listCanonicalConversations(
		serverId: string,
		page?: TaggedConversationsPageQuery
	): Promise<CanonicalConversationPage>;
	/** GET /api/v1/chat/conversations/{id}?server_id=… — safe header + gap honesty. */
	getConversation(conversationId: string, serverId: string): Promise<CanonicalConversationDetail>;
	/**
	 * GET /api/v1/chat/conversations/{id}/messages?server_id=… — role-preserving
	 * sanitized transcript page ordered `(sequence, message_id)`, same-filter total.
	 */
	listConversationMessages(
		conversationId: string,
		serverId: string,
		page?: PageQuery
	): Promise<CanonicalTranscriptPage>;
	/**
	 * GET /api/v1/chat/conversations/{id}/events?server_id=&after_seq=&limit= —
	 * bounded cursor-exclusive metadata events plus the current-state block.
	 */
	listConversationEvents(
		conversationId: string,
		serverId: string,
		query?: ConversationEventsQuery
	): Promise<CanonicalEventsPage>;
	/**
	 * POST /api/v1/chat/conversations — create a conversation and admit its
	 * first turn (idempotent on `client_request_id`: 201 created, 200
	 * deduplicated; the body's `disposition` is the honest verdict).
	 */
	createConversation(payload: CanonicalConversationTurn): Promise<CanonicalTurnSubmitted>;
	/**
	 * POST /api/v1/chat/conversations/{id}/messages — one follow-up turn onto
	 * an existing conversation (201 created / 200 deduplicated / 202
	 * queued_followup while another run holds the writer lease).
	 */
	submitConversationMessage(
		conversationId: string,
		payload: CanonicalConversationTurn
	): Promise<CanonicalTurnSubmitted>;
	/**
	 * POST /api/v1/chat/conversations/{id}/steer — one deterministic
	 * steering/control request onto the active run (201 created, 200
	 * deduplicated on retransmission).
	 */
	steerConversation(
		conversationId: string,
		payload: ConversationSteerRequest
	): Promise<ConversationSteerView>;

	// Runtime v2 objective surface (§11; activity-ui). Every method takes
	// its explicit `server_id` (C01 rule 2); collection reads are bounded
	// pages (AGENTS.md §1); mutations cross the session→origin→CSRF
	// boundary exactly like every other method here.
	/** GET /api/v1/objectives?server_id=… — bounded page of safe summaries. */
	listObjectives(
		serverId: string,
		page?: PageQuery & { status?: ObjectiveStatus }
	): Promise<ObjectiveListPage>;
	/** GET /api/v1/objectives/{id}?server_id=… — scope-checked detail. */
	readObjective(objectiveId: string, serverId: string): Promise<ObjectiveDetail>;
	/** GET /api/v1/objectives/{id}/journal?server_id=… — derived work journal. */
	readObjectiveJournal(objectiveId: string, serverId: string): Promise<WorkJournal>;
	/** GET /api/v1/objectives/{id}/events?server_id=… — bounded ledger page. */
	listObjectiveEvents(
		objectiveId: string,
		serverId: string,
		query?: ObjectiveEventsQuery
	): Promise<ObjectiveEventPage>;
	/** GET /api/v1/objectives/{id}/runs?server_id=… — bounded run-bindings page. */
	listObjectiveRuns(
		objectiveId: string,
		serverId: string,
		page?: PageQuery
	): Promise<ObjectiveRunPage>;
	/** GET /api/v1/objectives/{id}/checklist?server_id=… — 404 when never maintained. */
	readObjectiveChecklist(objectiveId: string, serverId: string): Promise<unknown>;
	/** POST /api/v1/objectives/{id}/scope — operator scope edit under CAS. */
	changeObjectiveScope(objectiveId: string, payload: components['schemas']['ObjectiveScopeBody']): Promise<ObjectiveDetail>;
	/** GET /api/v1/objectives/drain-status — legacy vs v2 drain inventory. */
	readDrainStatus(): Promise<DrainStatusView>;
	/**
	 * POST /api/v1/chat/conversations/{id}/resume — state-specific resume
	 * (§9): parked rejections arrive as structured 409 envelopes; a settled
	 * conversation's resume is the next submitted turn (201/200).
	 */
	resumeConversation(
		conversationId: string,
		payload: ConversationResumeBody
	): Promise<CanonicalTurnSubmitted>;
	/**
	 * POST /api/v1/chat/conversations/{id}/reconcile — operator
	 * classification of open unknown effects; the ONLY path that closes an
	 * `outcome_unknown` row from the HTTP surface (§9).
	 */
	reconcileConversation(
		conversationId: string,
		payload: ConversationReconcileBody
	): Promise<ConversationReconcileView>;

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
	/** GET /api/v1/chat/conversations/{id}/tags?server_id=… — bounded page. */
	listConversationTags(
		conversationId: string,
		serverId: string,
		page?: TagEntityTagsQuery
	): Promise<ConversationTagsPage>;
	/** POST /api/v1/chat/conversations/{id}/tags — idempotent user assignment. */
	assignConversationTag(conversationId: string, payload: TagAssignBody): Promise<void>;
	/** DELETE /api/v1/chat/conversations/{id}/tags/{tag_id}?server_id=… — removal. */
	removeConversationTag(
		conversationId: string,
		tagId: string,
		options: TagRemovalOptions
	): Promise<void>;

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
		payload: SessionTurnSubmitRequest
	): Promise<SessionTurnSubmitOutcome>;
	/**
	 * POST /api/v1/agent-sessions/{id}/control — steer/pause/cancel/
	 * approve/deny/reconcile through the reviewed session-runner facade.
	 */
	submitAgentSessionControl(
		sessionId: string,
		payload: SessionControlRequest
	): Promise<SessionControlOutcome>;
	/** GET /api/v1/agent-sessions/{id}/tags?server_id=… — bounded tag page. */
	listAgentSessionTags(
		sessionId: string,
		serverId: string,
		page?: TagEntityTagsQuery
	): Promise<SessionTagsPage>;
	/** POST /api/v1/agent-sessions/{id}/tags — idempotent user assignment. */
	assignAgentSessionTag(sessionId: string, payload: TagAssignBody): Promise<void>;
	/** DELETE /api/v1/agent-sessions/{id}/tags/{tag_id}?server_id=… — removal. */
	removeAgentSessionTag(
		sessionId: string,
		tagId: string,
		options: TagRemovalOptions
	): Promise<void>;

	// Task surface (§8; 15-task-api). Every method takes its explicit
	// `server_id` (C01 rule 2); every collection read is a bounded page
	// (AGENTS.md §1); the two mutations are CAS + idempotency-key guarded
	// (201 created / 200 deduplicated / structured 409 revision_conflict /
	// scope-blind 404 task_not_found). Plan review (revisions), decisions
	// and operation approvals (the C03 surface) stay three distinct routes.
	/**
	 * GET /api/v1/chat/conversations/{id}/task?server_id=… — the
	 * conversation's ONE active task header (revision, phase, status,
	 * counts); absent conversation, foreign scope and absent episode all
	 * collapse into the SAME scope-blind 404.
	 */
	getConversationTask(conversationId: string, serverId: string): Promise<TaskView>;
	/**
	 * GET /api/v1/chat/tasks/{id}/items?server_id=… — deterministic
	 * `(position, step_id)` plan-step page; superseded rows are recorded P3
	 * history, served only when `includeSuperseded` asks for them.
	 */
	listTaskSteps(
		taskId: string,
		serverId: string,
		query?: TaskCollectionQuery
	): Promise<TaskStepsPage>;
	/**
	 * GET /api/v1/chat/tasks/{id}/constraints?server_id=… — deterministic
	 * `(position, id)` constraint page carrying origin (user vs model) and
	 * source-message anchors.
	 */
	listTaskConstraints(
		taskId: string,
		serverId: string,
		query?: TaskCollectionQuery
	): Promise<TaskConstraintsPage>;
	/**
	 * GET /api/v1/chat/tasks/{id}/evidence?server_id=… — provenance
	 * identities only (kind + referenced record id + safe note); the route
	 * never inlines the referenced content.
	 */
	listTaskEvidence(taskId: string, serverId: string, page?: PageQuery): Promise<TaskEvidencePage>;
	/**
	 * GET /api/v1/chat/tasks/{id}/revisions?server_id=… — the append-only
	 * shared revision ledger, newest first (the P5 reconnect truth: UI and
	 * agent tools re-derive the same server revision from it).
	 */
	listTaskRevisions(
		taskId: string,
		serverId: string,
		page?: PageQuery
	): Promise<TaskRevisionsPage>;
	/**
	 * POST /api/v1/chat/tasks/{id}/revisions — one authenticated user
	 * correction/proposal: CAS `base_revision` (stale = structured 409
	 * revision_conflict, never last-writer-wins), `idempotency_key`
	 * (retransmission replays onto the SAME revision, 200 deduplicated),
	 * mandatory source-linked `source_message_id`. Always author="user".
	 */
	submitTaskCorrection(taskId: string, payload: TaskCorrectionBody): Promise<TaskMutationOutcome>;
	/**
	 * POST /api/v1/chat/tasks/{id}/decisions — one explicit user decision:
	 * advisory phase selection or pause/cancel (frozen transitions). Never
	 * applies a plan delta, never authorizes or terminates an operation.
	 */
	submitTaskDecision(taskId: string, payload: TaskDecisionBody): Promise<TaskMutationOutcome>;

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

		// Conversations (P06).
		submitMessage: (payload) => requestJson<ChatCreated>('POST', '/api/v1/chat/messages', payload),
		listConversations: (serverId, page) => {
			const scope =
				serverId === undefined || serverId === null
					? ''
					: `server_id=${encodeURIComponent(serverId)}`;
			return requestJson<ChatPage>(
				'GET',
				`/api/v1/chat/runs?${scope}${pageSuffix(page)}`
			);
		},
		readConversation: (runId) =>
			requestJson<ConversationView>('GET', `/api/v1/chat/runs/${encodeURIComponent(runId)}`),
		renameConversation: (runId, payload) =>
			requestJson<ConversationView>(
				'PATCH',
				`/api/v1/chat/runs/${encodeURIComponent(runId)}`,
				payload
			),
		cancelConversation: (runId, payload) =>
			requestJson<CancelView>(
				'POST',
				`/api/v1/chat/runs/${encodeURIComponent(runId)}/cancel`,
				payload
			),

		// Canonical conversation surface (§3; 09-client-continuity). Same
		// transport as every other method: C01 envelope errors through
		// `ApiError`, session cookies + CSRF double-submit on mutations.
		// The W6B tag filter rides the reviewed endpoint; the repeated
		// tag_id list is appended before the page suffix so both encodings
		// stay honest.
		listCanonicalConversations: (serverId, page) => {
			const tagFilter = new URLSearchParams();
			for (const tagId of page?.tagIds ?? []) tagFilter.append('tag_id', tagId);
			if (page?.tagMatch !== undefined) tagFilter.set('tag_match', page.tagMatch);
			const rendered = tagFilter.toString();
			return requestJson<CanonicalConversationPage>(
				'GET',
				`/api/v1/chat/conversations?server_id=${encodeURIComponent(serverId)}${rendered === '' ? '' : `&${rendered}`}${pageSuffix(page)}`
			);
		},
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
		listConversationTags: (conversationId, serverId, page) =>
			requestJson<ConversationTagsPage>(
				'GET',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/tags?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		assignConversationTag: (conversationId, payload) =>
			requestVoid(
				'POST',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/tags`,
				payload,
				204
			),
		removeConversationTag: (conversationId, tagId, options) => {
			const params = new URLSearchParams({ server_id: options.serverId });
			if (options.replacementTagId !== undefined && options.replacementTagId !== '')
				params.set('replacement_tag_id', options.replacementTagId);
			return requestVoid(
				'DELETE',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/tags/${encodeURIComponent(tagId)}?${params.toString()}`,
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
		submitAgentSessionTurn: (sessionId, payload) =>
			requestJson<SessionTurnSubmitOutcome>(
				'POST',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}/turns`,
				payload
			),
		submitAgentSessionControl: (sessionId, payload) =>
			requestJson<SessionControlOutcome>(
				'POST',
				`/api/v1/agent-sessions/${encodeURIComponent(sessionId)}/control`,
				payload
			),
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
		getConversation: (conversationId, serverId) =>
			requestJson<CanonicalConversationDetail>(
				'GET',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}?server_id=${encodeURIComponent(serverId)}`
			),
		listConversationMessages: (conversationId, serverId, page) =>
			requestJson<CanonicalTranscriptPage>(
				'GET',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/messages?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		listConversationEvents: (conversationId, serverId, query) => {
			const params = new URLSearchParams();
			params.set('server_id', serverId);
			if (query?.afterSeq !== undefined) params.set('after_seq', String(query.afterSeq));
			if (query?.limit !== undefined) params.set('limit', String(query.limit));
			return requestJson<CanonicalEventsPage>(
				'GET',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/events?${params.toString()}`
			);
		},
		createConversation: (payload) =>
			requestJson<CanonicalTurnSubmitted>('POST', '/api/v1/chat/conversations', payload),
		submitConversationMessage: (conversationId, payload) =>
			requestJson<CanonicalTurnSubmitted>(
				'POST',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/messages`,
				payload
			),
		steerConversation: (conversationId, payload) =>
			requestJson<ConversationSteerView>(
				'POST',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/steer`,
				payload
			),

		// Runtime v2 objective surface (§11). Same transport as every other
		// method: C01 envelope errors through `ApiError`, session cookies +
		// CSRF double-submit on the scope mutation, bounded pages with the
		// explicit server scope.
		listObjectives: (serverId, page) => {
			const params = new URLSearchParams({ server_id: serverId });
			if (page?.limit !== undefined) params.set('limit', String(page.limit));
			if (page?.offset !== undefined) params.set('offset', String(page.offset));
			if (page?.status !== undefined) params.set('status', page.status);
			return requestJson<ObjectiveListPage>('GET', `/api/v1/objectives?${params.toString()}`);
		},
		readObjective: (objectiveId, serverId) =>
			requestJson<ObjectiveDetail>(
				'GET',
				`/api/v1/objectives/${encodeURIComponent(objectiveId)}?server_id=${encodeURIComponent(serverId)}`
			),
		readObjectiveJournal: (objectiveId, serverId) =>
			requestJson<WorkJournal>(
				'GET',
				`/api/v1/objectives/${encodeURIComponent(objectiveId)}/journal?server_id=${encodeURIComponent(serverId)}`
			),
		listObjectiveEvents: (objectiveId, serverId, query) => {
			const params = new URLSearchParams({ server_id: serverId });
			if (query?.limit !== undefined) params.set('limit', String(query.limit));
			if (query?.offset !== undefined) params.set('offset', String(query.offset));
			if (query?.type !== undefined && query.type !== '') params.set('type', query.type);
			if (query?.afterSeq !== undefined) params.set('after_seq', String(query.afterSeq));
			return requestJson<ObjectiveEventPage>(
				'GET',
				`/api/v1/objectives/${encodeURIComponent(objectiveId)}/events?${params.toString()}`
			);
		},
		listObjectiveRuns: (objectiveId, serverId, page) =>
			requestJson<ObjectiveRunPage>(
				'GET',
				`/api/v1/objectives/${encodeURIComponent(objectiveId)}/runs?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		readObjectiveChecklist: (objectiveId, serverId) =>
			requestJson<unknown>(
				'GET',
				`/api/v1/objectives/${encodeURIComponent(objectiveId)}/checklist?server_id=${encodeURIComponent(serverId)}`
			),
		changeObjectiveScope: (objectiveId, payload) =>
			requestJson<ObjectiveDetail>(
				'POST',
				`/api/v1/objectives/${encodeURIComponent(objectiveId)}/scope`,
				payload
			),
		readDrainStatus: () => requestJson<DrainStatusView>('GET', '/api/v1/objectives/drain-status'),
		resumeConversation: (conversationId, payload) =>
			requestJson<CanonicalTurnSubmitted>(
				'POST',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/resume`,
				payload
			),
		reconcileConversation: (conversationId, payload) =>
			requestJson<ConversationReconcileView>(
				'POST',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/reconcile`,
				payload
			),

		// Task surface (§8; 15-task-api). Same transport as every other
		// method: session cookies, CSRF double-submit on the two mutations,
		// C01 envelope errors through `ApiError`.
		getConversationTask: (conversationId, serverId) =>
			requestJson<TaskView>(
				'GET',
				`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/task?server_id=${encodeURIComponent(serverId)}`
			),
		listTaskSteps: (taskId, serverId, query) =>
			requestJson<TaskStepsPage>(
				'GET',
				`/api/v1/chat/tasks/${encodeURIComponent(taskId)}/items?server_id=${encodeURIComponent(serverId)}${taskQuerySuffix(query)}`
			),
		listTaskConstraints: (taskId, serverId, query) =>
			requestJson<TaskConstraintsPage>(
				'GET',
				`/api/v1/chat/tasks/${encodeURIComponent(taskId)}/constraints?server_id=${encodeURIComponent(serverId)}${taskQuerySuffix(query)}`
			),
		listTaskEvidence: (taskId, serverId, page) =>
			requestJson<TaskEvidencePage>(
				'GET',
				`/api/v1/chat/tasks/${encodeURIComponent(taskId)}/evidence?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		listTaskRevisions: (taskId, serverId, page) =>
			requestJson<TaskRevisionsPage>(
				'GET',
				`/api/v1/chat/tasks/${encodeURIComponent(taskId)}/revisions?server_id=${encodeURIComponent(serverId)}${pageSuffix(page)}`
			),
		submitTaskCorrection: (taskId, payload) =>
			requestJson<TaskMutationOutcome>(
				'POST',
				`/api/v1/chat/tasks/${encodeURIComponent(taskId)}/revisions`,
				payload
			),
		submitTaskDecision: (taskId, payload) =>
			requestJson<TaskMutationOutcome>(
				'POST',
				`/api/v1/chat/tasks/${encodeURIComponent(taskId)}/decisions`,
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
