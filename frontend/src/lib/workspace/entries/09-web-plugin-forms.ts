/**
 * Feature-owned panel entry — card `mai-09-web-plugin-forms` (P09, service
 * `web`).
 *
 * This module is the ONE registration point of the plugin-forms panel
 * (windowed-workspace ADR §7.1: a fixed trusted component map; registration
 * grants no permissions). It owns:
 *
 *  - the stable panel slug and the typed wire shapes the panel consumes
 *    (aliases of the generated `schema.generated.ts` — never restated
 *    fields, never invented endpoints);
 *  - the typed services bag the panel requires, mirroring the reviewed
 *    producers 1:1: the plugin configuration surface (`09-plugin-api`,
 *    `backend/src/mai/api/plugins.py`: catalog, config read/edit/delete,
 *    declared schema, server grant bindings, bounded MCP discovery) and
 *    the skills read surface (`09-skills`: value-free catalog + package
 *    with the prerequisite binding report, C03 rule 7);
 *  - nothing else. The install/configure/enable distinction is the
 *    producer's frozen vocabulary and stays distinct on the wire
 *    (`catalog.installed` distribution metadata vs a `configured` stored
 *    document vs the document's `enabled` flag — C03 rule 7: no
 *    installation-time auto-grant, a binding is its own explicit write).
 *
 * The skills surface is a read-only consumer of the SAME reviewed API as
 * the plugin surface; the panel uses it for inspection and the
 * declared-minus-granted prerequisite report. `null` (or an incomplete
 * object) fails closed to a visible "not wired" note — never a fake read.
 *
 * No module-level mutable state; safe to evaluate per SSR request (ADR §5
 * rule 4). Composition passes
 * `pluginFormsServices({ …clientPluginMethods }, skillsOrNull)` — the
 * composed client grows the plugins/skills transport methods; instances
 * still share nothing.
 */

import type { components } from '../../api/schema.generated.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const PLUGIN_FORMS_PANEL_KIND = 'plugin-forms';

// ---------------------------------------------------------------------------
// Generated wire shapes (schema.generated.ts; never restated fields)
// ---------------------------------------------------------------------------

/** Generated wire shape: availability and grants as separate sections. */
export type PluginCatalogView = components['schemas']['PluginCatalogView'];
/** Generated wire shape: one configured plugin document + its bindings. */
export type ConfiguredPluginView = components['schemas']['ConfiguredPluginView'];
/** Generated wire shape: one stored plugin connection document (parsed). */
export type PluginConfigView = components['schemas']['PluginConfigView'];
/** Generated wire shape: one versioned config edit (full-document replace). */
export type PluginConfigEditBody = components['schemas']['PluginConfigEditBody'];
/** Generated wire shape: the declared configuration schema of one plugin. */
export type PluginSchemaView = components['schemas']['PluginSchemaView'];
/** Generated wire shape: one explicit server grant binding document. */
export type BindingView = components['schemas']['BindingView'];
/** Generated wire shape: one versioned server grant binding edit. */
export type PluginBindingEditBody = components['schemas']['PluginBindingEditBody'];
/** Generated wire shape: post-commit version triple of one record write. */
export type RevisionTriple = components['schemas']['RevisionTriple'];
/** Generated wire shape: one bounded MCP discovery report (value-free). */
export type McpDiscoveryReportView = components['schemas']['McpDiscoveryReportView'];
/** Generated wire shape: one value-free per-tool discovery rejection. */
export type McpToolRejectionView = components['schemas']['McpToolRejectionView'];
/** Generated wire shape: the value-free skills catalog (loads + rejections). */
export type SkillCatalogView = components['schemas']['SkillCatalogView'];
/** Generated wire shape: one loadable skill package summary. */
export type SkillSummaryView = components['schemas']['SkillSummaryView'];
/** Generated wire shape: one full skill package + prerequisite report. */
export type SkillPackageView = components['schemas']['SkillPackageView'];
/** Generated wire shape: one bounded, digest-stamped skill document. */
export type SkillDocumentView = components['schemas']['SkillDocumentView'];
/** Generated wire shape: one value-free per-package load rejection. */
export type SkillRejectionView = components['schemas']['SkillRejectionView'];

// ---------------------------------------------------------------------------
// Services bag (mirrors the reviewed routes 1:1; opener supplies it)
// ---------------------------------------------------------------------------

/**
 * The plugins API slice this panel consumes, injected via the panel
 * services bag (satisfied by the composed client once it grows the P09
 * transport methods). Mirrors the reviewed routes 1:1; reads are
 * session-checked server-side, mutations additionally origin+CSRF checked
 * (auth ADR §5) — the bag never weakens that. Every method names its
 * plugin (and, for bindings, the server) explicitly; there is no
 * module-level or client-level "current server" anywhere (C01 rule 2,
 * NT5).
 */
export interface PluginsApi {
	/** GET /api/v1/plugins — availability + configured documents, separately. */
	catalog(): Promise<PluginCatalogView>;
	/** GET /api/v1/plugins/{plugin}/config — the stored document, refs only. */
	readConfig(plugin: string): Promise<PluginConfigView>;
	/**
	 * PUT /api/v1/plugins/{plugin}/config — one versioned full-document
	 * edit validated against the declared schema (C01 rule 6: the caller's
	 * `expected_revision`; 0 creates).
	 */
	editConfig(plugin: string, payload: PluginConfigEditBody): Promise<RevisionTriple>;
	/** DELETE /api/v1/plugins/{plugin}/config?expected_revision= — refused while bindings exist. */
	removeConfig(plugin: string, expectedRevision: number): Promise<void>;
	/** GET /api/v1/plugins/{plugin}/schema — the schema-generated-form input. */
	readSchema(plugin: string): Promise<PluginSchemaView>;
	/** GET /api/v1/plugins/{plugin}/bindings — the explicit grant documents. */
	listBindings(plugin: string): Promise<BindingView[]>;
	/** PUT /api/v1/plugins/{plugin}/bindings/{server_id} — grant explicitly. */
	editBinding(
		plugin: string,
		serverId: string,
		payload: PluginBindingEditBody
	): Promise<RevisionTriple>;
	/** DELETE /api/v1/plugins/{plugin}/bindings/{server_id}?expected_revision= — revoke one grant. */
	removeBinding(plugin: string, serverId: string, expectedRevision: number): Promise<void>;
	/** POST /api/v1/plugins/{plugin}/discovery — one bounded MCP discovery. */
	runDiscovery(plugin: string): Promise<McpDiscoveryReportView>;
}

/**
 * The skills read slice (09-skills producer): the value-free catalog and
 * one full package with the declared-minus-granted prerequisite report.
 * `null` (or an incomplete object) fails closed in the panel — the skills
 * section shows the honest unavailable note and never fakes a read.
 */
export interface SkillsApi {
	/** GET /api/v1/skills — loaded summaries + surfaced rejections. */
	catalog(): Promise<SkillCatalogView>;
	/** GET /api/v1/skills/{skill} — one package + the binding report. */
	readSkill(skill: string): Promise<SkillPackageView>;
}

/** Typed services bag the plugin-forms panel requires from its opener. */
export interface PluginFormsServices {
	readonly plugins: PluginsApi;
	/**
	 * The skills read seam. `null` fails closed: the skills tab renders the
	 * honest unavailable note; the plugin surfaces stay fully usable.
	 */
	readonly skills: SkillsApi | null;
}

/** The feature entry for composition into the app's one panel registry. */
export function pluginFormsEntry(): PanelEntryInput {
	return {
		panel_kind: PLUGIN_FORMS_PANEL_KIND,
		title: 'MCP & Plugins',
		scope_types: ['global', 'server'],
		component: () =>
			import('../../components/PluginConfigForm.svelte').then((module) => module.default),
		default_geometry: { x: 72, y: 64, width: 900, height: 640 },
		min_geometry: { x: 0, y: 0, width: 480, height: 360 },
		// One instance per scope (one global controller window and one per server).
		singleton: true,
		capabilities_note:
			'Schema-generated plugin configuration forms, MCP server registration and ' +
			'bounded discovery, explicit server grant bindings, skills inspection; ' +
			'references only, registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `pluginFormsServices({ …clientPluginMethods }, clientSkillsOrNull)` into
 * its panel hosts). Freezing keeps the bag read-only from the panel's
 * side; the panel still fails closed if the bag is missing or incomplete.
 */
export function pluginFormsServices(
	plugins: PluginsApi,
	skills: SkillsApi | null = null
): PluginFormsServices {
	return Object.freeze({ plugins, skills });
}
