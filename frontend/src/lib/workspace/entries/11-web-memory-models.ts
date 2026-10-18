/**
 * Feature-owned panel entry — card `mai-11-web-memory-models` (P11, service
 * `web`). Contracts C01/C03/C06/C07/C08/C11. The app composes feature
 * entries into ONE `createPanelRegistry(...)` call — no per-feature shell
 * edit is needed, and this module stays the only place that names the panel
 * kind, its scope policy, its services contract and its component loader.
 * The loader is a statically analyzable lazy import FUNCTION (never a path
 * string); `createPanelRegistry` re-opens and validates this input, so a
 * malformed entry fails at composition.
 *
 * Scope: server. Hindsight memory is bank-per-server plus explicitly named
 * shared banks (C07 rule 6) and the local-model surface targets one SSH
 * host — every method below names the pinned server explicitly; there is no
 * module-level or client-level "current server" anywhere (C01 rule 2, NT5).
 *
 * Registration is not an authorization grant (ADR §7.1): `capabilities_note`
 * is descriptive metadata only. Every producer route re-checks session,
 * origin/CSRF and its grant class (`memory.recall` / `memory.retain` /
 * `memory.reflect` / `model.install`) server-side; this bag never weakens
 * that.
 *
 * Secret boundary (C03 rule 6): no function below accepts or returns a
 * credential VALUE. The only credential-adjacent vocabulary is the stored
 * connection's `credential_ref` — the `.env` variable NAME (or the absent
 * reference of an unauthenticated local endpoint) — and the value-free
 * `op://` reference shape the optional 1Password provider resolves
 * host-side (`mai.credentials.onepassword`; strict subset grammar, ≤400
 * chars, three non-empty segments). The panel renders reference NAMES and
 * shape classifications only; a secret value is never entered, sent,
 * stored, displayed or logged on this surface.
 */

import type { components } from '../../api/schema.generated.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const KNOWLEDGE_MODELS_PANEL_KIND = 'knowledge-models';

// ---------------------------------------------------------------------------
// Generated wire shapes (schema.generated.ts; never restated fields)
// ---------------------------------------------------------------------------

/** Generated wire shape: recall request (scope + bounded query vocabulary). */
export type MemoryRecallBody = components['schemas']['MemoryRecallBody'];
/** Generated wire shape: one synchronous retain item (producer-validated). */
export type MemoryRetainItemBody = components['schemas']['MemoryRetainItemBody'];
/** Generated wire shape: retain request (scope + items, real receipt). */
export type MemoryRetainBody = components['schemas']['MemoryRetainBody'];
/** Generated wire shape: reflect request (bounded generated answer). */
export type MemoryReflectBody = components['schemas']['MemoryReflectBody'];
/** Generated wire shape: delete request (explicit target, never inferred). */
export type MemoryDeleteBody = components['schemas']['MemoryDeleteBody'];
/** Generated wire shape: prerequisite preview request (identity+recipe+target). */
export type ModelLocalPreviewBody = components['schemas']['ModelLocalPreviewBody'];
/** Generated wire shape: preview plan + REAL measured prerequisite facts. */
export type ModelLocalPreviewView = components['schemas']['ModelLocalPreviewView'];
/** Generated wire shape: install submit body (plan rebuilt server-side). */
export type ModelLocalSubmitBody = components['schemas']['ModelLocalSubmitBody'];
/** Generated wire shape: pull submit body (acquisition + registration). */
export type ModelLocalPullBody = components['schemas']['ModelLocalPullBody'];
/** Generated wire shape: submit outcome (run view + dedup verdict, C05 rule 5). */
export type RunCreated = components['schemas']['RunCreated'];
/** Generated wire shape: one run plus its durable step intent rows. */
export type RunDetail = components['schemas']['RunDetail'];
/** Generated wire shape: one bounded page of stored connection references. */
export type ModelConnectionList = components['schemas']['ModelConnectionList'];
/** Generated wire shape: stored connection view (credential reference only). */
export type ModelConnectionView = components['schemas']['ModelConnectionView'];

// ---------------------------------------------------------------------------
// Frozen memory documents (producer hand-backs)
//
// The memory routes answer plain JSONResponse documents, so the OpenAPI
// declarations are `unknown`; these narrow types document the actual
// observed bodies, frozen by `backend/src/mai/memory/hindsight.py`
// (`health_document` / `recall_document` / `retain_document` /
// `reflect_document` / `delete_document`) and the export route in
// `backend/src/mai/api/knowledge_models.py`. Field-for-field mirrors —
// never restated semantics.
// ---------------------------------------------------------------------------

/** `status: "ok" | "unavailable"` — the honest answer of one memory op. */
export type MemoryStatusKind = 'ok' | 'unavailable';

/** One availability probe (no bank content is touched). */
export interface MemoryStatusDocument {
	status: MemoryStatusKind;
	code: string | null;
	provider: string;
	operation: 'status';
	api_version: string | null;
	error_type: string | null;
}

/** One recalled hit: excerpt plus citation/provenance/freshness (C07 rule 4). */
export interface MemoryRecallHit {
	rank: number;
	id: string;
	citation: string;
	bank: string;
	text: string;
	truncated: boolean;
	type: string;
	occurred_start: string | null;
	occurred_end: string | null;
	mentioned_at: string | null;
	document_id: string | null;
	entities: string[];
	tags: string[];
	score: number;
}

/** One recall outcome; empty `results` is truthful ONLY when status is ok. */
export interface MemoryRecallDocument {
	status: MemoryStatusKind;
	code: string | null;
	provider: string;
	operation: 'recall';
	trust: string;
	role: string;
	scope: string;
	bank: string;
	query: string;
	results: MemoryRecallHit[];
	results_truncated: boolean;
	error_type: string | null;
}

/** The retain receipt of one synchronous write (KB-H2: never fabricated). */
export interface MemoryRetainDocument {
	status: MemoryStatusKind;
	code: string | null;
	provider: string;
	operation: 'retain';
	scope: string;
	bank: string;
	receipt: {
		items_count: number;
		operation_id: string;
		usage: {
			input_tokens: number;
			output_tokens: number;
			total_tokens: number;
		};
	} | null;
	error_type: string | null;
}

/** One bounded generated answer grounded in the scoped bank. */
export interface MemoryReflectDocument {
	status: MemoryStatusKind;
	code: string | null;
	provider: string;
	operation: 'reflect';
	trust: string;
	role: string;
	scope: string;
	bank: string;
	query: string;
	text: string;
	truncated: boolean;
	based_on: string[];
	error_type: string | null;
}

/** One explicit, receipted delete (`operation` is `delete-memories`|`delete-bank`). */
export interface MemoryDeleteDocument {
	status: MemoryStatusKind;
	code: string | null;
	provider: string;
	operation: string;
	scope: string;
	bank: string;
	deleted_count: number | null;
	error_type: string | null;
}

/** One bank manifest export (backup-grade read; the manifest IS the data). */
export interface MemoryExportDocument {
	status: MemoryStatusKind;
	code: string | null;
	provider: string;
	operation: 'export';
	manifest?: unknown;
	error_type: string | null;
}

// ---------------------------------------------------------------------------
// Services bag (mirrors the reviewed routes 1:1; opener supplies it)
// ---------------------------------------------------------------------------

/**
 * The memory/local-model API slice this panel consumes, injected via the
 * panel services bag (satisfied by the composed client once it grows the
 * transport methods). Delete/export routes carry the scope explicitly; the
 * submit routes name the run idempotency key client-side (C05 rule 5).
 */
export interface KnowledgeModelsApi {
	/** GET /api/v1/servers/{id}/memory/status — availability probe. */
	memoryStatus(serverId: string): Promise<MemoryStatusDocument>;
	/** POST /api/v1/servers/{id}/memory/recall — gated cited recall. */
	recallMemory(serverId: string, body: MemoryRecallBody): Promise<MemoryRecallDocument>;
	/** POST /api/v1/servers/{id}/memory/retain — synchronous real receipt. */
	retainMemory(serverId: string, body: MemoryRetainBody): Promise<MemoryRetainDocument>;
	/** POST /api/v1/servers/{id}/memory/reflect — bounded grounded answer. */
	reflectMemory(serverId: string, body: MemoryReflectBody): Promise<MemoryReflectDocument>;
	/** GET /api/v1/servers/{id}/memory/export?scope=… — backup-grade read. */
	exportMemory(serverId: string, scope: string): Promise<MemoryExportDocument>;
	/** POST /api/v1/servers/{id}/memory/delete — explicit, receipted. */
	deleteMemory(serverId: string, body: MemoryDeleteBody): Promise<MemoryDeleteDocument>;
	/** POST /api/v1/servers/{id}/models/local/preview — effect-free measurement. */
	previewModelInstall(
		serverId: string,
		body: ModelLocalPreviewBody
	): Promise<ModelLocalPreviewView>;
	/** POST /api/v1/servers/{id}/models/local/{app}/install — durable intent. */
	installModelService(
		serverId: string,
		applicationId: string,
		body: ModelLocalSubmitBody
	): Promise<RunCreated>;
	/** POST /api/v1/servers/{id}/models/local/{app}/pull — pull IS registration. */
	pullModelWeights(
		serverId: string,
		applicationId: string,
		body: ModelLocalPullBody
	): Promise<RunCreated>;
	/** GET /api/v1/jobs/runs/{run_id} — replayed run + durable steps. */
	readRun(runId: string): Promise<RunDetail>;
	/** GET /api/v1/models/connections — value-free connection references. */
	listConnections(): Promise<ModelConnectionList>;
	/** GET /api/v1/models/connections/{name} — credential reference only. */
	readConnection(name: string): Promise<ModelConnectionView>;
}

/**
 * The panel-links seam: opening the separately implemented Applications,
 * Jobs and Model-settings panels is THEIR feature surface's job (windowing
 * is host-owned, ADR §7.1). The panel only requests the open. `null` or an
 * incomplete member fails closed to a visible note — never a fake
 * navigation.
 */
export interface KnowledgeModelsLinks {
	/** Open the applications inventory for one server (adopted app id). */
	openApplications(serverId: string): void;
	/** Open the runs/schedules management panel for one server. */
	openJobs(serverId: string): void;
	/** Open the model-settings panel (registered connection editing). */
	openModelSettings(): void;
}

/** Typed services bag the knowledge-models panel requires from its opener. */
export interface KnowledgeModelsServices {
	readonly api: KnowledgeModelsApi;
	/** `null` fails closed: link controls render only when wired. */
	readonly links: KnowledgeModelsLinks | null;
}

/** The feature entry for composition into the app's one panel registry. */
export function knowledgeModelsEntry(): PanelEntryInput {
	return {
		panel_kind: KNOWLEDGE_MODELS_PANEL_KIND,
		title: 'Memory & local models',
		scope_types: ['server'],
		component: () =>
			import('../../components/KnowledgeAndModels.svelte').then((module) => module.default),
		default_geometry: { x: 56, y: 48, width: 920, height: 640 },
		min_geometry: { x: 0, y: 0, width: 560, height: 400 },
		// One desktop holds one memory/local-models panel per pinned server:
		// banks and model targets are per-server resources.
		singleton: true,
		capabilities_note:
			'Hindsight memory status/recall/retain/reflect/export/delete for the pinned server and ' +
			'shared banks; local-model preview/install/pull (the pull registers); references only.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `knowledgeModelsServices({ …memoryAndLocalModelMethods }, links)` into
 * its panel hosts). Freezing keeps the bag read-only from the panel's side;
 * the panel still fails closed if the bag is missing or incomplete.
 */
export function knowledgeModelsServices(
	api: KnowledgeModelsApi,
	links: KnowledgeModelsLinks | null = null
): KnowledgeModelsServices {
	return Object.freeze({ api, links });
}
