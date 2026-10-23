/**
 * Feature-owned panel entry — card `uir-u4-runbooks-apps` (the standalone
 * Global Runbook Catalog desktop window, under `Global / Fleet`).
 *
 * The Runbook Catalog window is the GLOBAL half of the applications split:
 * server-agnostic runbooks (identity, category, description and the five
 * operational instruction slots) live in `/api/v1/applications/catalog`,
 * while per-server state (`installed`, `custom_instructions`) stays on the
 * server's Applications window. This entry deliberately mirrors the
 * reviewed P08/P12 contract shape (ADR §7.1: one feature = one
 * registration point, typed services bag, opener supplies it; the bag
 * never weakens server-side auth checks — reads are session-checked,
 * mutations additionally origin+CSRF checked).
 */

import type { components } from '../../api/schema.generated.js';
import type { MaiApiClient } from '../../api/client.js';
import type { PageQuery } from './05-web-jobs.js';
import type { PanelContext, PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const RUNBOOKS_PANEL_KIND = 'runbooks';

/** Window title of the standalone Runbook Catalog desktop window. */
export const RUNBOOKS_PANEL_TITLE = 'Runbook Catalog';

// ---------------------------------------------------------------------------
// Generated wire shapes (schema.generated.ts; never restated fields)
// ---------------------------------------------------------------------------

/** Generated wire shape: one global catalog runbook. */
export type RunbookView = components['schemas']['RunbookView'];
/** Generated wire shape: one bounded catalog page + the merged taxonomy. */
export type RunbookCatalogPage = components['schemas']['RunbookCatalogPage'];
/** Generated wire shape: one catalog runbook upsert body. */
export type RunbookUpsertBody = components['schemas']['RunbookUpsertBody'];
/** Generated wire shape: one background runbook execution request. */
export type RunbookExecuteBody = components['schemas']['RunbookExecuteBody'];
/** Generated wire shape: one queued background runbook job. */
export type RunbookExecutionView = components['schemas']['RunbookExecutionView'];
/** Generated wire shape: one taxonomy category (built-in or custom). */
export type ServiceCategoryView = components['schemas']['ServiceCategoryView'];
/** Generated wire shape: the five operational runbook slots. */
export type ServiceInstructionsView = components['schemas']['ServiceInstructionsView'];

/** Optional bounded-page query of the catalog list route (C01 rule 5). */
export interface CatalogPageQuery {
	/** Page size; server default 50, maximum 200 (applications.py bounds). */
	limit?: number;
	/** Page offset. */
	offset?: number;
}

// ---------------------------------------------------------------------------
// Services bag (mirrors the reviewed routes 1:1; opener supplies it)
// ---------------------------------------------------------------------------

/**
 * The catalog API slice the Runbook Catalog window consumes, injected via
 * the panel services bag and satisfied by the composed client.
 */
export interface RunbookCatalogApi {
	/** GET /api/v1/applications/catalog — one bounded catalog page. */
	listRunbooks(page?: CatalogPageQuery): Promise<RunbookCatalogPage>;
	/** POST /api/v1/applications/catalog — upsert one global runbook. */
	upsertRunbook(payload: RunbookUpsertBody): Promise<RunbookView>;
	/** DELETE /api/v1/applications/catalog/{runbook_id} — delete the recipe only. */
	deleteRunbook(runbookId: string): Promise<void>;
}

/** The exact method vocabulary `readRunbookCatalogServices` fails closed on. */
const RUNBOOK_CATALOG_METHODS = ['listRunbooks', 'upsertRunbook', 'deleteRunbook'] as const;

/**
 * Narrow the opaque `panel.services` bag to the typed catalog slice.
 * A missing/incomplete bag yields `null` — the panel renders its honest
 * fail-closed note and never substitutes a fallback client.
 */
export function readRunbookCatalogServices(panel: PanelContext): RunbookCatalogApi | null {
	const candidate = (panel.services as Record<string, unknown>)[
		'runbookCatalog'
	] as Partial<RunbookCatalogApi> | undefined;
	if (typeof candidate !== 'object' || candidate === null) return null;
	for (const name of RUNBOOK_CATALOG_METHODS) {
		if (typeof (candidate as Record<string, unknown>)[name] !== 'function') return null;
	}
	return candidate as RunbookCatalogApi;
}

/**
 * Typed services bag factory for openers: maps the ONE composed client
 * onto the catalog window's exact contract. Freezing keeps the bag
 * read-only from the panel's side.
 */
export function runbookCatalogServices(api: MaiApiClient): {
	readonly runbookCatalog: RunbookCatalogApi;
} {
	return Object.freeze({
		runbookCatalog: Object.freeze({
			listRunbooks: api.listCatalogRunbooks,
			upsertRunbook: api.upsertCatalogRunbook,
			deleteRunbook: api.deleteCatalogRunbook
		})
	});
}

/** The five frozen runbook slots, in display order (shared contract). */
export const RUNBOOK_STEPS = Object.freeze([
	'install',
	'update',
	'uninstall',
	'backup',
	'verify'
] as const);
export type RunbookStep = (typeof RUNBOOK_STEPS)[number];

/** The feature entry for composition into the app's one panel registry. */
export function runbooksEntry(): PanelEntryInput {
	return {
		panel_kind: RUNBOOKS_PANEL_KIND,
		title: RUNBOOKS_PANEL_TITLE,
		scope_types: ['global'],
		component: () =>
			import('../../panels/RunbookCatalogPanel.svelte').then((module) => module.default),
		default_geometry: { x: 96, y: 64, width: 880, height: 640 },
		min_geometry: { x: 0, y: 0, width: 480, height: 360 },
		// One global window per desktop: the catalog is fleet-wide.
		singleton: true,
		capabilities_note:
			'Browses and edits the global Runbook Catalog (name, category, ' +
			'description, five instruction slots); no server state, grants or ' +
			'host effects.'
	};
}
