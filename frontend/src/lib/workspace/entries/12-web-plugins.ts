/**
 * Feature-owned panel entry — card `s4-plugins-window` (the standalone
 * Plugins desktop window, sibling of `mai-09-web-plugin-forms`).
 *
 * The Plugins window is the native-plugin surface: rich installed-plugin
 * cards (starting with 1Password Vault), secret readiness, enable/disable,
 * value-free connection probes, `default_vault`/`allowed_vaults`
 * configuration with write-only credential values, and one-click agent
 * grants (global `00000000-0000-0000-0000-000000000000` or per server).
 * MCP registration/skills inspection stay in the `plugin-forms` window —
 * this entry deliberately mirrors the reviewed P09 contract shape (ADR
 * §7.1: one feature = one registration point, typed services bag, opener
 * supplies it; the bag never weakens server-side auth checks).
 */

import type { components } from '../../api/schema.generated.js';
import type { MaiApiClient, ProfileResponse } from '../../api/client.js';
import type { PageQuery } from './05-web-jobs.js';
import type { PanelContext, PanelEntryInput, PanelServices } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const PLUGINS_PANEL_KIND = 'plugins';

/** Window title of the standalone Plugins desktop window. */
export const PLUGINS_PANEL_TITLE = 'Plugins';

// ---------------------------------------------------------------------------
// Generated wire shapes (schema.generated.ts; never restated fields)
// ---------------------------------------------------------------------------

/** Generated wire shape: the bounded plugin catalogue page. */
export type PluginCatalogView = components['schemas']['PluginCatalogView'];
/** Generated wire shape: one installed plugin's rich metadata view. */
export type InstalledPluginView = components['schemas']['InstalledPluginView'];
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
/** Generated wire shape: one value-free readiness probe result. */
export type PluginProbeView = components['schemas']['PluginProbeView'];
/** Generated wire shape: one optional readiness-probe input (vault name). */
export type PluginProbeBody = components['schemas']['PluginProbeBody'];
/** Generated wire shape: post-commit version triple of one record write. */
export type RevisionTriple = components['schemas']['RevisionTriple'];
/** Generated wire shape: one enrolled server profile (binding target). */
export type ServerProfileView = ProfileResponse;

// ---------------------------------------------------------------------------
// Services bag (mirrors the reviewed routes 1:1; opener supplies it)
// ---------------------------------------------------------------------------

/**
 * The plugins API slice the standalone Plugins window consumes, injected
 * via the panel services bag and satisfied by the composed client. Reads
 * are session-checked server-side, mutations additionally origin+CSRF
 * checked — the bag never weakens that. Every method names its plugin
 * (and, for bindings, the server) explicitly; there is no module-level
 * or client-level "current server" anywhere (C01 rule 2, NT5).
 */
export interface PluginsPanelServices {
	/** GET /api/v1/plugins — bounded catalogue page (limit/offset/total). */
	pluginCatalog(page?: PageQuery): Promise<PluginCatalogView>;
	/** GET /api/v1/plugins/{plugin}/config — the stored document, refs only. */
	getPluginConfig(plugin: string): Promise<PluginConfigView>;
	/** PUT /api/v1/plugins/{plugin}/config — versioned full-document edit. */
	putPluginConfig(plugin: string, payload: PluginConfigEditBody): Promise<RevisionTriple>;
	/** DELETE /api/v1/plugins/{plugin}/config?expected_revision= — refused while bindings exist. */
	deletePluginConfig(plugin: string, expectedRevision: number): Promise<void>;
	/** GET /api/v1/plugins/{plugin}/schema — the declared config schema. */
	getPluginSchema(plugin: string): Promise<PluginSchemaView>;
	/** GET /api/v1/plugins/{plugin}/bindings — the explicit grant documents. */
	listPluginBindings(plugin: string): Promise<BindingView[]>;
	/** PUT /api/v1/plugins/{plugin}/bindings/{server_id} — grant explicitly. */
	putPluginBinding(
		plugin: string,
		serverId: string,
		payload: PluginBindingEditBody
	): Promise<RevisionTriple>;
	/** DELETE /api/v1/plugins/{plugin}/bindings/{server_id}?expected_revision= — revoke one grant. */
	deletePluginBinding(plugin: string, serverId: string, expectedRevision: number): Promise<void>;
	/** POST /api/v1/plugins/{plugin}/probe — value-free readiness check. */
	probePlugin(plugin: string, body?: PluginProbeBody): Promise<PluginProbeView>;
	/** GET /api/v1/servers — enrolled binding targets (identity only). */
	listServers(): Promise<ProfileResponse[]>;
}

/** The exact method vocabulary `readPluginsServices` fails closed on. */
const PLUGINS_PANEL_METHODS = [
	'pluginCatalog',
	'getPluginConfig',
	'putPluginConfig',
	'deletePluginConfig',
	'getPluginSchema',
	'listPluginBindings',
	'putPluginBinding',
	'deletePluginBinding',
	'probePlugin',
	'listServers'
] as const;

/**
 * Narrow the opaque `panel.services` bag to the typed plugins slice.
 * A missing/incomplete bag yields `null` — the panel renders its honest
 * fail-closed note and never substitutes a fallback client.
 */
export function readPluginsServices(panel: PanelContext): PluginsPanelServices | null {
	const candidate = (panel.services as Record<string, unknown>)[
		'plugins'
	] as Partial<PluginsPanelServices> | undefined;
	if (typeof candidate !== 'object' || candidate === null) return null;
	for (const name of PLUGINS_PANEL_METHODS) {
		if (typeof (candidate as Record<string, unknown>)[name] !== 'function') return null;
	}
	return candidate as PluginsPanelServices;
}

/**
 * Typed services bag factory for openers: maps the ONE composed client
 * onto the Plugins window's exact contract (the `listServers` seam wraps
 * the client's profile listing down to the enrolled target identities).
 * Freezing keeps the bag read-only from the panel's side.
 */
export function pluginsPanelServices(api: MaiApiClient): { readonly plugins: PluginsPanelServices } {
	// Nested under the `'plugins'` key `readPluginsServices` reads from
	// `panel.services` (same bag convention as the other feature entries).
	return Object.freeze({
		plugins: Object.freeze({
			pluginCatalog: api.pluginCatalog,
			getPluginConfig: api.readPluginConfig,
			putPluginConfig: api.editPluginConfig,
			deletePluginConfig: api.removePluginConfig,
			getPluginSchema: api.readPluginSchema,
			listPluginBindings: api.listPluginBindings,
			putPluginBinding: api.editPluginBinding,
			deletePluginBinding: api.removePluginBinding,
			probePlugin: api.probePlugin,
			listServers: async (): Promise<ProfileResponse[]> => (await api.listProfiles()).servers
		})
	});
}

/** The feature entry for composition into the app's one panel registry. */
export function pluginsEntry(): PanelEntryInput {
	return {
		panel_kind: PLUGINS_PANEL_KIND,
		title: PLUGINS_PANEL_TITLE,
		scope_types: ['global', 'server'],
		component: () =>
			import('../../panels/PluginsPanel.svelte').then((module) => module.default),
		default_geometry: { x: 88, y: 68, width: 920, height: 660 },
		min_geometry: { x: 0, y: 0, width: 480, height: 360 },
		// One instance per scope (one global window and one per server).
		singleton: true,
		capabilities_note:
			'Native plugin cards with secret readiness and value-free connection probes, ' +
			'default_vault/allowed_vaults configuration with write-only credential values, ' +
			'explicit agent grant bindings (global or per server); grants never follow ' +
			'from configuring, enabling or probing.'
	};
}
