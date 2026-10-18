/**
 * Feature-owned panel entry — card `mai-03-web-policy` (P03, service `web`).
 *
 * The single registration point of the capability-policy panel (windowed
 * workspace ADR-0001 §7.1; contracts C03/C08/C11): the app composes feature
 * entries into ONE `createPanelRegistry(...)` call — no per-feature shell edit
 * is needed, and this module stays the only place that names the panel kind,
 * its scope policy, its services contract and its component loader. The loader
 * is a statically analyzable lazy import FUNCTION (never a path string);
 * `createPanelRegistry` re-opens and validates this input, so a malformed
 * entry fails at composition.
 *
 * Registration is not an authorization grant (ADR §7.1): `capabilities_note`
 * is descriptive metadata that no code path reads, and approving a pending
 * operation through this panel authorizes exactly that one bound operation —
 * it never grants a tool (C03 rule 4).
 *
 * Scope policy: `global` only — the grant configuration IS global policy
 * (one revision counter, `mai.api.grants`); the panel reads no launcher
 * selection and pins no server (ADR §3.2: a global view must never inherit
 * the last-selected server's identity). `singleton: true` deduplicates per
 * global scope (`singletonInstanceKey`), so one desktop cannot open two
 * policy editors; the stale-edit conflict across two writers is handled by
 * the panel's revision-checked save (C01 rule 6), not by instance policy.
 *
 * Services contract: the four functions below mirror exactly the reviewed
 * P03 producers (`mai.api.grants` GET/PUT `/api/v1/grants`, `mai.api.approvals`
 * GET `/api/v1/approvals/{approval_id}` and POST
 * `/api/v1/approvals/{approval_id}/decision`) and the generated
 * `schema.generated.ts` operations. Wire types are re-exported aliases of the
 * generated components — no invented endpoints or shapes (C08 rule 8). The
 * transport is injected through the panel services bag, exactly as
 * `02-web-server.ts` injects the profile API; `createMaiApiClient()` gains the
 * four one-line transport methods at desktop composition (outside this card's
 * allowlist), and any object with this shape satisfies the panel fail-closed
 * narrowing.
 */

import type { components } from '../../api/schema.generated.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const CAPABILITY_POLICY_PANEL_KIND = 'capability-policy';

/** Generated wire shape: the committed configuration (C02 rule-4 triple). */
export type GrantsConfigView = components['schemas']['GrantsConfigView'];
/** Generated wire shape: one versioned edit body (`expected_revision` guard). */
export type GrantsEditBody = components['schemas']['GrantsEditBody'];
/** Generated wire shape: post-commit version triple of one policy write. */
export type GrantsMutationResult = components['schemas']['GrantsMutationResult'];
/** Generated wire shape: one persisted decision (safe preview + receipt). */
export type ApprovalView = components['schemas']['ApprovalView'];
/** Generated wire shape: the binding the admin echoes field-for-field (C03 rule 4). */
export type DecisionBody = components['schemas']['DecisionBody'];

/**
 * The slice of the MAI API client the panel consumes, injected via the panel
 * services bag (satisfied by `createMaiApiClient()` once it grows the P03
 * transport methods; mirrors the reviewed routes 1:1).
 */
export interface CapabilityPolicyApi {
	/** GET /api/v1/grants — the committed policy + grants (404 before first commit). */
	readGrants(): Promise<GrantsConfigView>;
	/** PUT /api/v1/grants — one versioned edit; the route derives revision+1. */
	editGrants(payload: GrantsEditBody): Promise<GrantsMutationResult>;
	/** GET /api/v1/approvals/{approval_id} — the safe preview of one decision. */
	readApproval(approvalId: string): Promise<ApprovalView>;
	/** POST /api/v1/approvals/{approval_id}/decision — the one-use consumption. */
	decideApproval(approvalId: string, payload: DecisionBody): Promise<ApprovalView>;
}

/** Typed services bag the capability-policy panel requires from its opener. */
export interface CapabilityPolicyServices {
	readonly api: CapabilityPolicyApi;
}

/** The feature entry for composition into the app's one panel registry. */
export function capabilityPolicyEntry(): PanelEntryInput {
	return {
		panel_kind: CAPABILITY_POLICY_PANEL_KIND,
		title: 'Capability policy',
		scope_types: ['global'],
		component: () =>
			import('../../panels/CapabilityPolicyPanel.svelte').then((module) => module.default),
		default_geometry: { x: 120, y: 88, width: 640, height: 480 },
		min_geometry: { x: 0, y: 0, width: 420, height: 320 },
		singleton: true,
		capabilities_note:
			'Views and edits the committed grants/deny list and decides one bound approval at a ' +
			'time; registration grants no permissions and a decision never widens a grant.'
	};
}

/**
 * Typed services bag factory for openers (the composed desktop passes
 * `capabilityPolicyServices(createMaiApiClient())` into its panel hosts).
 * Freezing keeps the bag read-only from the panel's side; the panel still
 * fails closed if the bag is missing or incomplete.
 */
export function capabilityPolicyServices(api: CapabilityPolicyApi): CapabilityPolicyServices {
	return Object.freeze({ api });
}
