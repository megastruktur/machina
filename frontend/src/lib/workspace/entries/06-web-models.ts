/**
 * Feature-owned panel entry — card `mai-06-web-models` (P06, service `web`).
 *
 * The single registration point of the model-settings panel (windowed
 * workspace ADR-0001 §7.1; contracts C06/C08/C11): the app composes feature
 * entries into ONE `createPanelRegistry(...)` call — no per-feature shell edit
 * is needed, and this module stays the only place that names the panel kind,
 * its scope policy, its services contract and its component loader. The loader
 * is a statically analyzable lazy import FUNCTION (never a path string);
 * `createPanelRegistry` re-opens and validates this input, so a malformed
 * entry fails at composition.
 *
 * Registration is not an authorization grant (ADR §7.1): `capabilities_note`
 * is descriptive metadata that no code path reads.
 *
 * Scope policy: `global` only — the model configuration surface (connections,
 * profiles, the stored selection) is global configuration (`mai.api.models`);
 * the panel reads no launcher selection and pins no server (ADR §3.2: a
 * global view must never inherit the last-selected server's identity).
 * `singleton: true` deduplicates per global scope (`singletonInstanceKey`),
 * so one desktop cannot open two model-settings editors; the stale-edit
 * conflict across two writers is handled by the panel's revision-checked
 * saves (C01 rule 6), not by instance policy.
 *
 * Services contract: the twelve functions below mirror exactly the reviewed
 * P06 producers (`mai.api.models`: GET/PUT/DELETE `/api/v1/models/connections/{name}`,
 * `/api/v1/models/profiles/{name}`, GET/PUT/DELETE `/api/v1/models/selection`
 * and POST `/api/v1/models/test`) and the generated `schema.generated.ts`
 * operations. Wire types are re-exported aliases of the generated components
 * — no invented endpoints or shapes (C08 rule 8). The transport is injected
 * through the panel services bag, exactly as `02-web-server.ts` injects the
 * profile API; `createMaiApiClient()` gains the twelve one-line transport
 * methods at desktop composition (outside this card's allowlist), and any
 * object with this shape satisfies the panel fail-closed narrowing.
 *
 * Credential stance (C03 rule 6, brief acceptance 1): `credential_ref` is a
 * REFERENCE (the `.env` name) and the only credential-related field on this
 * entire surface. No function below accepts or returns a credential value,
 * and the panel never displays one — resolution happens exclusively inside
 * the producer's probe round.
 */

import type { components } from '../../api/schema.generated.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const MODEL_SETTINGS_PANEL_KIND = 'model-settings';

/** Generated wire shape: one value-free record reference (name + triple). */
export type ModelRecordRef = components['schemas']['ModelRecordRef'];
/** Generated wire shape: connection upsert body (`expected_revision` guard). */
export type ModelConnectionBody = components['schemas']['ModelConnectionBody'];
/** Generated wire shape: stored connection view (credential *reference* only). */
export type ModelConnectionView = components['schemas']['ModelConnectionView'];
/** Generated wire shape: one bounded page of stored connection references. */
export type ModelConnectionList = components['schemas']['ModelConnectionList'];
/** Generated wire shape: model-profile upsert body (allowlisted settings). */
export type ModelProfileBody = components['schemas']['ModelProfileBody'];
/** Generated wire shape: stored model-profile document view. */
export type ModelProfileView = components['schemas']['ModelProfileView'];
/** Generated wire shape: one bounded page of stored profile references. */
export type ModelProfileList = components['schemas']['ModelProfileList'];
/** Generated wire shape: explicit (connection, profile) selection body. */
export type ModelSelectionBody = components['schemas']['ModelSelectionBody'];
/** Generated wire shape: the stored selection (names the pair, pins no revisions). */
export type ModelSelectionView = components['schemas']['ModelSelectionView'];
/** Generated wire shape: bounded probe request (names optional, tools flag). */
export type ModelProbeBody = components['schemas']['ModelProbeBody'];
/** Generated wire shape: one classified, value-free probe outcome. */
export type ModelProbeOutcome = components['schemas']['ModelProbeOutcome'];
/** Generated wire shape: post-commit version triple of one record write. */
export type ModelMutationResult = components['schemas']['ModelMutationResult'];

/**
 * The slice of the MAI API client the panel consumes, injected via the panel
 * services bag (satisfied by `createMaiApiClient()` once it grows the P06
 * transport methods; mirrors the reviewed routes 1:1). Delete routes carry
 * `expectedRevision` as the documented `expected_revision` query parameter.
 */
export interface ModelSettingsApi {
	/** GET /api/v1/models/connections — value-free references. */
	listConnections(): Promise<ModelConnectionList>;
	/** GET /api/v1/models/connections/{name} — credential reference only. */
	readConnection(name: string): Promise<ModelConnectionView>;
	/** PUT /api/v1/models/connections/{name} — revision-checked upsert. */
	putConnection(name: string, payload: ModelConnectionBody): Promise<ModelMutationResult>;
	/** DELETE /api/v1/models/connections/{name}?expected_revision=… */
	deleteConnection(name: string, expectedRevision: number): Promise<void>;
	/** GET /api/v1/models/profiles — value-free references. */
	listProfiles(): Promise<ModelProfileList>;
	/** GET /api/v1/models/profiles/{name} — allowlisted settings view. */
	readProfile(name: string): Promise<ModelProfileView>;
	/** PUT /api/v1/models/profiles/{name} — revision-checked upsert. */
	putProfile(name: string, payload: ModelProfileBody): Promise<ModelMutationResult>;
	/** DELETE /api/v1/models/profiles/{name}?expected_revision=… */
	deleteProfile(name: string, expectedRevision: number): Promise<void>;
	/** GET /api/v1/models/selection — 404 when none is stored. */
	readSelection(): Promise<ModelSelectionView>;
	/** PUT /api/v1/models/selection — validated pair, optimistic revision. */
	putSelection(payload: ModelSelectionBody): Promise<ModelSelectionView>;
	/** DELETE /api/v1/models/selection?expected_revision=… */
	deleteSelection(expectedRevision: number): Promise<void>;
	/** POST /api/v1/models/test — one bounded classified probe round. */
	probe(payload: ModelProbeBody): Promise<ModelProbeOutcome>;
}

/** Typed services bag the model-settings panel requires from its opener. */
export interface ModelSettingsServices {
	readonly api: ModelSettingsApi;
}

/** The feature entry for composition into the app's one panel registry. */
export function modelSettingsEntry(): PanelEntryInput {
	return {
		panel_kind: MODEL_SETTINGS_PANEL_KIND,
		title: 'Model settings',
		scope_types: ['global'],
		component: () =>
			import('../../panels/ModelSettingsPanel.svelte').then((module) => module.default),
		default_geometry: { x: 120, y: 88, width: 680, height: 520 },
		min_geometry: { x: 0, y: 0, width: 480, height: 360 },
		singleton: true,
		capabilities_note:
			'Edits stored model connections/profiles and the explicit selection by name and runs the ' +
			'bounded probe; credential references only — registration grants no permissions.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `modelSettingsServices(createMaiApiClient())` into its panel hosts).
 * Freezing keeps the bag read-only from the panel's side; the panel still
 * fails closed if the bag is missing or incomplete.
 */
export function modelSettingsServices(api: ModelSettingsApi): ModelSettingsServices {
	return Object.freeze({ api });
}
