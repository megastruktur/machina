/**
 * Feature-owned panel entry — card `mai-02-web-server` (P02, service `web`).
 *
 * The single registration point of the server-profile panel (windowed
 * workspace ADR-0001 §7.1; contracts C11): the app composes feature entries
 * into ONE `createPanelRegistry(...)` call — no per-feature shell edit is
 * needed, and this module stays the only place that names the panel kind, its
 * scope policy, its services contract and its component loader. The loader is
 * a statically analyzable lazy import FUNCTION (never a path string);
 * `createPanelRegistry` re-opens and validates this input, so a malformed
 * entry fails at composition.
 *
 * Registration is not an authorization grant (ADR §7.1): `capabilities_note`
 * is descriptive metadata that no code path reads.
 *
 * Scope policy: `server` only, unpinned — every instance edits exactly the
 * server pinned at open time (`scope.server_id`); there is no global variant
 * and no object pin. `singleton: true` deduplicates per full pinned scope
 * (`singletonInstanceKey`), so one desktop cannot open two editors for the
 * same server; different servers remain independent windows (NT5 target
 * separation). Cross-tab staleness is handled by the panel's revision-checked
 * save (C01 rule 6), not by instance policy.
 */

import type { ProfileEdit, ProfileMutationResult, ProfileResponse } from '../../api/client.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const SERVER_PROFILE_PANEL_KIND = 'server-profile';

/**
 * The slice of the MAI API client the panel consumes, injected via the panel
 * services bag (satisfied by `createMaiApiClient()` from `02-api-client`).
 */
export interface ServerProfileApi {
	/** GET /api/v1/servers/{server_id} — explicit target, never a selection. */
	readProfile(serverId: string): Promise<ProfileResponse>;
	/** PUT /api/v1/servers/{server_id} — revision-checked edit (C01 rule 6). */
	editProfile(serverId: string, payload: ProfileEdit): Promise<ProfileMutationResult>;
}

/** Typed services bag the server-profile panel requires from its opener. */
export interface ServerProfileServices {
	readonly api: ServerProfileApi;
}

/** The feature entry for composition into the app's one panel registry. */
export function serverProfileEntry(): PanelEntryInput {
	return {
		panel_kind: SERVER_PROFILE_PANEL_KIND,
		title: 'Server profile',
		scope_types: ['server'],
		component: () =>
			import('../../panels/ServerProfilePanel.svelte').then((module) => module.default),
		default_geometry: { x: 96, y: 64, width: 560, height: 420 },
		min_geometry: { x: 0, y: 0, width: 380, height: 280 },
		singleton: true,
		capabilities_note:
			'Views one pinned server profile and edits its prompt edition through the ' +
			'authenticated profile API; registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `serverProfileServices(createMaiApiClient())` into its panel hosts).
 * Freezing keeps the bag read-only from the panel's side; the panel still
 * fails closed if the bag is missing or incomplete.
 */
export function serverProfileServices(api: ServerProfileApi): ServerProfileServices {
	return Object.freeze({ api });
}
