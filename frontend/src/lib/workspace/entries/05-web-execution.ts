/**
 * Feature-owned panel entry — card `mai-05-web-execution` (P05, service `web`).
 *
 * The single registration point of the execution panel (windowed workspace
 * ADR-0001 §7.1; contracts C04/C05/C08/C11): the app composes feature entries
 * into ONE `createPanelRegistry(...)` call — no per-feature shell edit is
 * needed, and this module stays the only place that names the panel kind, its
 * scope policy, its services contract and its component loader. The loader is
 * a statically analyzable lazy import FUNCTION (never a path string);
 * `createPanelRegistry` re-opens and validates this input, so a malformed
 * entry fails at composition.
 *
 * Registration is not an authorization grant (ADR §7.1): `capabilities_note`
 * is descriptive metadata that no code path reads. Submitting a run is gated
 * by the committed grants/policy on the worker side (C03); this panel adds no
 * bypass route and cannot widen any check (SERVICES "End-to-end execution
 * route": HTTP never executes, the intent route is the one door).
 *
 * Scope policy: `server` only, unpinned — every instance executes against
 * exactly the server pinned at open time (`scope.server_id`); there is no
 * global variant and no object pin (C01 rule 2: every operation receives an
 * explicit `server_id`). `singleton: true` deduplicates per full pinned scope
 * (`singletonInstanceKey`), so one desktop cannot open two execution panels
 * for the same target; different targets remain independent windows, and a
 * launcher re-selection never retargets a tracked run or a bound password
 * challenge (C08 rule 5, NT5).
 *
 * Services contract: the six functions below mirror exactly the reviewed
 * producers — `mai.api.servers` GET `/api/v1/servers/{server_id}`, the
 * `mai.api.jobs` facade (POST `/api/v1/jobs/runs`, GET
 * `/api/v1/jobs/runs/{run_id}`, POST `/api/v1/jobs/runs/{run_id}/cancel`),
 * `mai.api.approvals` GET `/api/v1/approvals/{approval_id}` and
 * `mai.api.password_challenges` POST `/api/v1/password-challenges` — plus the
 * generated `schema.generated.ts` operations. Wire types are re-exported
 * aliases of the generated components — no invented endpoints or shapes
 * (C08 rule 8). The transport is injected through the panel services bag,
 * exactly as `02-web-server.ts`/`03-web-policy.ts` inject theirs;
 * `createMaiApiClient()` gains these one-line transport methods at desktop
 * composition (outside this card's allowlist), and any object with this shape
 * satisfies the panel fail-closed narrowing.
 *
 * The concrete SSE event subscription is deliberately NOT part of this bag:
 * `subscribeEvents` in `api/client.ts` is the generic read primitive, and the
 * concrete jobs-stream consumer is `05-web-jobs`' exclusive resource. This
 * panel tracks its one run through the bounded `readRun` replay (state is
 * replayed server-side, never engine-called), so no duplicate event
 * consumption path is introduced.
 */

import type { components } from '../../api/schema.generated.js';
import type { PanelEntryInput } from '../registry.js';

/** Stable panel slug (ADR §7.1); validated against the registry on open. */
export const EXECUTION_PANEL_KIND = 'execution';

/** The one capability this panel submits (host-supervision lane, C04). */
export const SHELL_CAPABILITY_ID = 'host.shell';

/** Generated wire shape: the submit body (client-minted idempotency key, C05 rule 5). */
export type RunSubmitBody = components['schemas']['RunSubmitBody'];
/** Generated wire shape: submit outcome (run view + dedup verdict). */
export type RunCreated = components['schemas']['RunCreated'];
/** Generated wire shape: one run plus its durable step rows. */
export type RunDetail = components['schemas']['RunDetail'];
/** Generated wire shape: explicit tree cancellation body (engine guard rule 4). */
export type CancelBody = components['schemas']['CancelBody'];
/** Generated wire shape: KNOWN engine outcome or an honest pending. */
export type CancelView = components['schemas']['CancelView'];
/** Generated wire shape: the safe preview of one bound decision. */
export type ApprovalView = components['schemas']['ApprovalView'];
/** Generated wire shape: the pinned challenge presentation (echoed field-for-field). */
export type ChallengeBody = components['schemas']['ChallengeBody'];
/** Generated wire shape: the frozen CommandOutcome view (C04 rule 4). */
export type ChallengeOutcomeView = components['schemas']['ChallengeOutcomeView'];
/** Generated wire shape: the enrolled target profile (identity + privilege). */
export type ProfileResponse = components['schemas']['ProfileResponse'];

/**
 * The slice of the MAI API client the panel consumes, injected via the panel
 * services bag (satisfied by `createMaiApiClient()` once it grows the P05
 * transport methods; mirrors the reviewed routes 1:1).
 */
export interface ExecutionApi {
	/** GET /api/v1/servers/{server_id} — the pinned target, never a selection. */
	readProfile(serverId: string): Promise<ProfileResponse>;
	/** POST /api/v1/jobs/runs — durable intent; HTTP never executes (C05). */
	submitRun(payload: RunSubmitBody): Promise<RunCreated>;
	/** GET /api/v1/jobs/runs/{run_id} — replayed state + durable steps. */
	readRun(runId: string): Promise<RunDetail>;
	/** POST /api/v1/jobs/runs/{run_id}/cancel — records the request once. */
	cancelRun(runId: string, payload: CancelBody): Promise<CancelView>;
	/** GET /api/v1/approvals/{approval_id} — the bound decision preview. */
	readApproval(approvalId: string): Promise<ApprovalView>;
	/** POST /api/v1/password-challenges — spends one decision, transports one secret. */
	submitChallenge(payload: ChallengeBody): Promise<ChallengeOutcomeView>;
}

/** Typed services bag the execution panel requires from its opener. */
export interface ExecutionServices {
	readonly api: ExecutionApi;
}

/** The feature entry for composition into the app's one panel registry. */
export function executionEntry(): PanelEntryInput {
	return {
		panel_kind: EXECUTION_PANEL_KIND,
		title: 'Execution',
		scope_types: ['server'],
		component: () =>
			import('../../components/ExecutionPanel.svelte').then((module) => module.default),
		default_geometry: { x: 72, y: 72, width: 640, height: 520 },
		min_geometry: { x: 0, y: 0, width: 460, height: 360 },
		singleton: true,
		capabilities_note:
			'Submits one durable run intent for the pinned target and renders its replayed ' +
			'state; grants and approval gates stay server-side; registration grants no ' +
			'permissions.'
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
