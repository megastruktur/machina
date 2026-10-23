/**
 * Feature-owned panel entry — card `mai-05-web-execution` (uir task
 * `u2-terminal`).
 *
 * The single registration point of the interactive Terminal panel (windowed
 * workspace ADR-0001 §7.1): the app composes feature entries into ONE
 * registry, and this module owns the `execution` kind — its title, allowed
 * scopes, geometry bounds and the typed services bag the panel body
 * consumes. The panel itself renders the interactive SSH terminal window
 * (xterm.js) against the pinned server scope; approvals, grants and secret
 * transport stay server-side, so opening this panel grants no permissions.
 */

import type { components } from '../../api/schema.generated.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const EXECUTION_PANEL_KIND = 'execution';

/** Generated wire shape: the authenticated terminal attach information. */
export type TerminalSessionInfoView = components['schemas']['TerminalSessionInfoView'];

/**
 * The slice of the MAI API client the panel consumes, injected via the panel
 * services bag (satisfied by `createMaiApiClient()` at composition).
 *
 * `openTerminalSocket` is optional: when the opener does not supply one the
 * panel falls back to the browser `WebSocket` constructor targeting
 * `/api/v1/servers/{server_id}/terminal` (the `mai_session` cookie rides
 * along automatically; the server validates session + Origin). Test harnesses
 * and alternative openers inject a factory instead of a global.
 */
export interface ExecutionApi {
	/** GET /api/v1/servers/{server_id}/terminal/info — the pinned target. */
	terminalInfo(serverId: string): Promise<TerminalSessionInfoView>;
	/** Open the terminal WebSocket; defaults to the browser constructor. */
	openTerminalSocket?(serverId: string): WebSocket;
}

/** Typed services bag the execution panel requires from its opener. */
export interface ExecutionServices {
	readonly api: ExecutionApi;
}

/** The feature entry for composition into the app's one panel registry. */
export function executionEntry(): PanelEntryInput {
	return {
		panel_kind: EXECUTION_PANEL_KIND,
		title: 'Terminal',
		scope_types: ['server'],
		component: () =>
			import('../../components/ExecutionPanel.svelte').then((module) => module.default),
		default_geometry: { x: 72, y: 72, width: 640, height: 520 },
		min_geometry: { x: 0, y: 0, width: 460, height: 360 },
		singleton: true,
		capabilities_note:
			'Opens an interactive SSH terminal to the pinned target over an ' +
			'authenticated socket; tmux attaches when present. Grants and approval ' +
			'gates stay server-side; registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `executionServices(createMaiApiClient())` into its panel hosts).
 * Freezing keeps the bag read-only from the panel's side; the panel still
 * fails closed if the bag is missing or incomplete.
 */
export function executionServices(api: ExecutionApi): ExecutionServices {
	return Object.freeze({ api });
}
