/**
 * Feature-owned panel entry — card `mai-07-web-docs` (P07, service `web`).
 *
 * This module is the ONE registration point of the documents panel
 * (windowed-workspace ADR §7.1: a fixed trusted component map; registration
 * grants no permissions). It owns:
 *
 *  - the stable panel slug and the typed wire shapes the panel consumes;
 *  - the typed services bag the panel requires, mirroring the reviewed
 *    producers 1:1: the documents surface (`07-docs-api`,
 *    `backend/src/mai/api/documents.py`) — the projection list/read, the
 *    revision-checked edit, cited hybrid retrieval, the durable index
 *    outbox + reconcile, run-evidence generation and the bounded
 *    deep-research pass;
 *  - the composition seam for the ONE reviewed conversation surface
 *    (`06-chat-api` via the `06-web-chat` entry): attaching an excerpt to
 *    chat is a real, durable `submitMessage` intent whose message carries
 *    the excerpt under its citation — no invented endpoint, no client-side
 *    excerpt store.
 *
 * Response views are narrowed locally because the producer answers with
 * value JSON (`JSONResponse` dicts, no response model): every interface
 * below mirrors ONE reviewed producer function verbatim and names it —
 * `_document_view`, `list_documents`, `index_status`/`_intent_view`,
 * `_outcome_view`, `retrieve.result_document`, `generate` preview/commit
 * bodies and `research.workflow.report_document`. Nothing is invented; the
 * fields are the producer's frozen key set.
 *
 * No module-level mutable state; safe to evaluate per SSR request (ADR §5
 * rule 4). Composition passes `docsServices({ …clientDocsMethods },
 * { submitMessage })` — the composed client grows the docs transport
 * methods; instances still share nothing.
 */

import type { components } from '../../api/schema.generated.js';
import type { PanelEntryInput } from '../registry.js';
import type { ChatCreated, ChatSubmitBody } from './06-web-chat.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const DOCS_PANEL_KIND = 'documents';

// ---------------------------------------------------------------------------
// Generated request/wire shapes (schema.generated.ts; never restated fields)
// ---------------------------------------------------------------------------

/** Generated wire shape: one canonical collection (`docs`/`runbooks`). */
export type DocNamespace = components['schemas']['DocumentNamespace'];
/** Generated wire shape: one C07 rule 5 verification vocabulary value. */
export type VerificationStatus =
    components['schemas']['mai__knowledge__documents__VerificationStatus'];
/** Generated wire shape: one scope's reindex-reconcile request. */
export type ReconcileBody = components['schemas']['ReconcileBody'];
/** Generated wire shape: one revision-checked full-content edit. */
export type DocumentEditBody = components['schemas']['DocumentEditBody'];
/** Generated wire shape: one bounded hybrid retrieval request. */
export type RetrievalBody = components['schemas']['RetrievalBody'];
/** Generated wire shape: one run-evidence generation request. */
export type GenerateBody = components['schemas']['GenerateBody'];
/** Generated wire shape: one server-scoped deep-research request. */
export type ResearchBody = components['schemas']['ResearchBody'];

/** Optional bounded-page query shared by the list routes (C01 rule 5). */
export interface DocsPageQuery {
	/** Page size; server default and maximum are producer-owned bounds. */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

// ---------------------------------------------------------------------------
// Producer response views (frozen JSON of documents.py / retrieve.py /
// generate.py / research/workflow.py — mirrored, never re-derived)
// ---------------------------------------------------------------------------

/**
 * One projection row of `GET /api/v1/docs` (`list_documents` response,
 * `knowledge_documents` projection; C02 W2: the designed UI read path —
 * the canonical file stays the authority).
 */
export interface DocumentListItem {
	doc_id: string;
	server_id: string | null;
	app_id: string | null;
	namespace: string;
	revision: number;
	sha256: string;
	citation: string;
	source_kind: string;
	origin: string | null;
	origin_revision: string | null;
	verification: string;
	source_path: string | null;
	observed_at: string;
}

/** The `list_documents` response: one scope page of live documents. */
export interface DocumentPage {
	server_id: string | null;
	app_id: string | null;
	namespace: string | null;
	documents: DocumentListItem[];
	total?: number;
	limit?: number;
	offset?: number;
}

/**
 * One stored document: `documents.py` `_document_view(with_text=True)` —
 * hash-verified read with verbatim provenance (C07 rule 1).
 */
export interface DocumentView {
	doc_id: string;
	server_id: string | null;
	app_id: string | null;
	namespace: string;
	revision: number;
	sha256: string;
	citation: string;
	source_kind: string;
	origin: string | null;
	origin_revision: string | null;
	verification: string;
	/** Present only when the read asked for text (`with_text=True`). */
	content?: string;
}

/** `_document_view(with_text=False)`: the head returned by edit/commit. */
export type DocumentHeadView = Omit<DocumentView, 'content'>;

/**
 * One durable outbox intent (`documents.py` `_intent_view`): pending
 * reindex work is explicit, never "unknown" (AC2).
 */
export interface IndexIntentView {
	doc_id: string;
	namespace: string;
	op: string;
	revision: number | null;
	sha256: string | null;
	tombstone: boolean;
	status: 'pending';
}

/**
 * One projection row of `GET /api/v1/docs/index` (`index_status`): the
 * indexed revision + chunk count of one live document.
 */
export interface IndexProjectionRow {
	doc_id: string;
	namespace: string;
	revision: number;
	sha256: string;
	chunks: number;
	tombstone: boolean;
	observed_at: string;
	status: 'indexed' | 'tombstoned';
}

/** The `index_status` response; `projection` names its unavailability. */
export interface IndexStatus {
	server_id: string | null;
	app_id: string | null;
	namespace: string | null;
	pending: IndexIntentView[];
	projection: IndexProjectionRow[] | 'unavailable';
}

/**
 * One per-intent reconcile outcome (`documents.py` `_outcome_view`): the
 * producer's stable vocabulary verbatim — `indexed`/`unchanged`/`deleted`/
 * `absent`/`pending` plus a stable failure `code` when pending.
 */
export interface ReconcileOutcomeView {
	doc_id: string;
	namespace: string;
	op: string;
	status: string;
	revision: number | null;
	chunks: number | null;
	code: string | null;
}

/** The `POST /api/v1/docs/index/reconcile` response. */
export interface ReconcileReport {
	server_id: string | null;
	app_id: string | null;
	outcomes: ReconcileOutcomeView[];
}

/**
 * One retrieval excerpt (`retrieve.py` `result_document` excerpt item):
 * the full citation tuple travels with every excerpt so truncated text
 * stays traceable (C07 rule 4).
 */
export interface RetrievalExcerpt {
	document_key: string;
	doc_id: string;
	namespace: string;
	revision: number;
	sha256: string;
	ord: number;
	citation: string;
	source_kind: string;
	origin: string;
	origin_revision: string | null;
	verification: string;
	observed_at: string | null;
	score: number;
	vector_rank: number | null;
	fts_rank: number | null;
	excerpt: string;
	truncated: boolean;
}

/**
 * The hybrid retrieval answer (`retrieve.py` `result_document`):
 * `status: "ok"` with zero excerpts means "ran, no match"; `status:
 * "unavailable"` names the failed seam via the stable `code` — never an
 * empty-success from a failed query (AC2).
 */
export interface RetrievalResult {
	status: 'ok' | 'unavailable';
	code: string | null;
	scope: string;
	server_id: string | null;
	excerpts: RetrievalExcerpt[];
	error_type: string | null;
}

/** The `POST /api/v1/docs/generate` preview body (no store touch). */
export interface GeneratePreviewView {
	preview: true;
	namespace: string;
	classification: string;
	verification: string;
	content: string;
}

/**
 * The `POST /api/v1/docs/generate` commit body: the created canonical
 * document plus the honest reindex outcome — `null` means "no indexer is
 * wired on this surface; the durable intent converges later", never a
 * fabricated `reindexed`.
 */
export interface GenerateCommitView {
	preview: false;
	doc: DocumentHeadView;
	classification: string;
	verification: string;
	reindex: ReconcileOutcomeView[] | null;
}

export type GenerateView = GeneratePreviewView | GenerateCommitView;

/** One recorded research source (`workflow.py` `report_document`). */
export interface ResearchSourceView {
	kind: string;
	citation: string;
	trust: string;
	status: string;
	truncated: boolean;
	error_type: string | null;
	artifact_refs: string[];
	text: string;
}

/** One recorded research step (`workflow.py` `report_document`). */
export interface ResearchStepView {
	seq: number;
	phase: string;
	capability_id: string;
	status: string;
	display: string;
	evidence_refs: string[];
}

/**
 * One settled deep-research report (`workflow.py` `report_document`):
 * sources and hypotheses stay distinct; a failed run carries exactly one
 * canonical incident reference (C07 rule 5).
 */
export interface ResearchReport {
	v: number;
	run_id: string;
	server_id: string;
	status: string;
	question: string;
	prompt_revision: string | null;
	prompt_sha256: string | null;
	profile_revision: string | null;
	error: string | null;
	steps: ResearchStepView[];
	sources: ResearchSourceView[];
	hypotheses: Array<{ claim: string; basis: string[] }>;
	dropped_claims: string[];
	incident: Record<string, unknown> | null;
	incident_error: string | null;
}

// ---------------------------------------------------------------------------
// Chat composition seam (06-chat-api legacy + D07 session-first attach)
// ---------------------------------------------------------------------------

/**
 * The durable admission outcome of one session-first attach: the exact
 * identities the server ack returned (BRIEF_D07 §2.4; sfc_PLAN D07 — typed
 * admission, idempotency, returned session_id/run_id).
 */
export interface SessionAttachOutcome {
	/** The session the excerpt turn was admitted into. */
	session_id: string;
	/** The subordinate run allocated for the turn. */
	run_id: string;
	/** The committed user entry carrying the excerpt. */
	entry_id: string;
	/** The entry's per-session monotonic sequence. */
	seq: number;
	/** True when a retry/duplicate click hit the same idempotency key. */
	deduplicated: boolean;
}

/**
 * One session-first attach request. `session_id === null/undefined` is the
 * EXPLICIT create-new-session action; a set id attaches to that explicit
 * destination session. One `client_request_id` per attach attempt: the
 * create and the turn are idempotent on it, so a duplicate click or retry
 * converges instead of duplicating (plan acceptance 3).
 */
export interface DocsAttachTurn {
	/** Explicit destination session; null ⇒ explicit create-new action. */
	session_id?: string | null;
	/** The composed excerpt message (citation + quoted excerpt + note). */
	text: string;
	/** Preferred model profile for the new run. */
	model_profile?: string | null;
	/** Client idempotency key (client-minted, one per attach attempt). */
	client_request_id: string;
}

/**
 * The chat seam the documents panel consumes. Session-first attach is the
 * D07 path (`attachToSession`); the legacy one-shot conversation submit
 * stays as the OPTIONAL fallback for pre-session hosts until D09 deletes it
 * (COORD-D02-01). A bag with neither fails closed.
 */
export interface DocsChatApi {
	/** Create-or-reuse the explicit session, then admit the excerpt turn. */
	attachToSession(payload: DocsAttachTurn): Promise<SessionAttachOutcome>;
	/** POST /api/v1/chat/messages — legacy intent; idempotent on `run_id`. */
	submitMessage?(payload: ChatSubmitBody): Promise<ChatCreated>;
}

/** Back-compat alias (the pre-D07 seam name); prefer `DocsChatApi`. */
export type ChatSubmit = DocsChatApi;

// ---------------------------------------------------------------------------
// Services bag (mirrors the reviewed routes 1:1; opener supplies it)
// ---------------------------------------------------------------------------

/**
 * The documents API slice this panel consumes, injected via the panel
 * services bag (satisfied by the composed client once it grows the P07
 * transport methods). Mirrors the reviewed routes 1:1; read-only routes
 * are session-checked server-side, mutations additionally origin+CSRF
 * checked (auth ADR §5) — the bag never weakens that. Every method names
 * its scope explicitly; there is no module-level or client-level "current
 * server" anywhere (C01 rule 2).
 */
export interface DocsApi {
	/** GET /api/v1/docs?server_id=… — one scope's live projection page. */
	listDocuments(serverId: string, page?: DocsPageQuery): Promise<DocumentPage>;
	/**
	 * GET /api/v1/docs/{doc_id}?server_id=… — hash-verified read + text.
	 * The producer resolves a missing namespace to `docs`, so a caller
	 * reading outside the default collection MUST pass it explicitly.
	 */
	readDocument(serverId: string, docId: string, namespace?: DocNamespace): Promise<DocumentView>;
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
}

/** Typed services bag the documents panel requires from its opener. */
export interface DocsServices {
	readonly api: DocsApi;
	/**
	 * The chat seam for "attach excerpt to chat" — session-first
	 * `attachToSession` (D07) with the legacy `submitMessage` fallback until
	 * D09. `null` (or a bag with neither method) fails closed: the panel
	 * renders the honest unavailable note and never fakes a submission.
	 */
	readonly chat: DocsChatApi | null;
}

/** The feature entry for composition into the app's one panel registry. */
export function docsEntry(): PanelEntryInput {
	return {
		panel_kind: DOCS_PANEL_KIND,
		title: 'Documents',
		scope_types: ['server'],
		// The panel browses documents of the pinned server; a window may
		// additionally pin ONE document object. Runs/conversations/applications
		// are other panels' pins.
		scope_object_kinds: ['document'],
		component: () => import('../../panels/DocumentsPanel.svelte').then((module) => module.default),
		default_geometry: { x: 64, y: 64, width: 820, height: 620 },
		min_geometry: { x: 0, y: 0, width: 480, height: 360 },
		// Free-instance: two windows may hold two different documents of one
		// server side by side; each instance is scoped by its own descriptor.
		singleton: false,
		capabilities_note:
			'Browses, edits and searches the pinned server\'s documents and reindex ' +
			'outbox; generates from run evidence and attaches excerpts to chat; ' +
			'registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `docsServices({ …clientDocsMethods }, docsChatAdapter)` into its panel
 * hosts). Freezing keeps the bag read-only from the panel's side; the panel
 * still fails closed if the bag is missing or incomplete.
 */
export function docsServices(api: DocsApi, chat: DocsChatApi | null = null): DocsServices {
	return Object.freeze({ api, chat });
}
