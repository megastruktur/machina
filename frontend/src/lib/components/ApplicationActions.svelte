<script module lang="ts">
	/**
	 * ApplicationActions — the install/update/remove lifecycle surface for ONE
	 * adopted application on ONE pinned server (card `mai-08-web-lifecycle`,
	 * P08; contracts C09 rule 3–5, C03 rule 4, C08 rule 6, C11).
	 *
	 * Layer: reusable feature component (windowed-workspace ADR §7.2). This is
	 * the surface the `ApplicationsLifecycle` seam of `08-web-apps` opens: the
	 * composed desktop mounts it in its own window per (server, application,
	 * mode); this card owns the component and its focused spec only — the
	 * registry entry and desktop wiring belong to the composition owner, and
	 * nothing here imports or mutates window state (no geometry, no drag, no
	 * focus/stacking, no stores, no module-level mutable state; every instance
	 * is self-contained and SSR-safe per ADR §5 rule 4).
	 *
	 * What it owns (brief acceptance 1/2):
	 *  1. **Bound plan preview (C09 rule 3)** — the pure `POST …/applications/plan`
	 *     route renders the ACTUAL intended resources: target identity, image,
	 *     compose path + hashes, directories, port bindings, persistent volumes
	 *     (the data impact), credential references, health probe, prerequisites,
	 *     required grants, planned operations and limitations. Nothing says
	 *     "will install" generically — the plan's own fields are the display.
	 *  2. **Approval-gated submit (C03 rule 4)** — install/update/remove submit
	 *     ONE durable `app.*` intent through the reviewed routes (HTTP persists
	 *     intent only; effects stay worker-side behind the G2 gate). The run
	 *     appears with its replayed facade state; a `waiting_approval` run
	 *     renders the step's approval binding (approval id, payload hash,
	 *     policy revision) and — when the composition wired the seam — a link
	 *     to the policy surface that holds the decision. A policy denial
	 *     surfaces as the executor's structured `refused` receipt, never as a
	 *     client-side guess.
	 *  3. **Two-tier data deletion (C09 rule 5)** — remove requires the
	 *     EXPLICIT `data_intent` (`keep` | `delete`) via a radio group with NO
	 *     default: an unmade decision cannot be submitted, an inferred absent
	 *     flag can never become delete. A recorded `delete` intent is exactly
	 *     that — recorded: tier-1 removal stops containers and tombstones the
	 *     canonical record and NEVER deletes persistent data; the tier-2 purge
	 *     is a separate operation behind its own one-use manifest-bound
	 *     approval. The surface renders that boundary verbatim for both
	 *     choices.
	 *  4. **Honest outcomes (C08 rule 6)** — the live run view is polled from
	 *     the reviewed run-detail route while the run is non-terminal.
	 *     `waiting_approval`, `reconciling`, `outcome_unknown`,
	 *     `cancel_requested` and `rolled_back` are rendered as exactly those
	 *     states — never as success. An unhealthy update (failed health gate,
	 *     rollback attempted/verified/unverified) shows its executor status and
	 *     reason verbatim.
	 *  5. **Linkage seams** — the run opens in the jobs surface and the
	 *     canonical record document opens in the documents surface through the
	 *     optional `links` bag (`openRun`/`openDocument`/`openApproval`),
	 *     each narrowed individually; an unwired link fails closed to displayed
	 *     identity text, never a fake action.
	 *
	 * Scope discipline (C01 rule 2, NT5): `serverId`/`applicationId` are
	 * explicit props — every outgoing call names them and every response is
	 * identity-checked before it may paint; nothing reads a global "current
	 * server". Hostile content (recipe text, producer reasons, paths) renders
	 * as inert text via escaped interpolation only — no `{@html}` anywhere.
	 * Async sections carry instance-local epoch guards: a late response of a
	 * superseded request (or of a disposed host) is dropped before it paints,
	 * and there is no client-side cache to go stale.
	 */
	/** The three lifecycle surfaces this component renders. */
	export type LifecycleMode = 'install' | 'update' | 'remove';
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import Button from '../ui/Button.svelte';
	import type { components } from '../api/schema.generated.js';

	/** Generated wire shape: pure plan preview request (C09 rule 3). */
	type PlanBody = components['schemas']['PlanBody'];
	/** Generated wire shape: the bound plan — actual resources and limitations. */
	type PlanView = components['schemas']['PlanView'];
	/** Generated wire shape: install submit body (plan rebuilt server-side). */
	type LifecycleSubmitBody = components['schemas']['LifecycleSubmitBody'];
	/** Generated wire shape: update submit body + current-recipe baseline assertion. */
	type UpdateSubmitBody = components['schemas']['UpdateSubmitBody'];
	/** Generated wire shape: remove submit body + EXPLICIT data intent. */
	type RemoveSubmitBody = components['schemas']['RemoveSubmitBody'];
	/** Generated wire shape: submit outcome — run view + dedup verdict (C05 r5). */
	type RunCreated = components['schemas']['RunCreated'];
	/** Generated wire shape: one run plus its durable step rows. */
	type RunDetail = components['schemas']['RunDetail'];
	/** Generated wire shape: one durable step row (intent + safe receipt). */
	type StepView = components['schemas']['StepView'];
	/** Generated wire shape: one persisted approval decision (safe preview). */
	type ApprovalView = components['schemas']['ApprovalView'];
	/** Generated wire shape: the adoption record-document outcome. */
	type RecordDocView = components['schemas']['RecordDocView'];

	/**
	 * The lifecycle API slice this component consumes, injected by its opener.
	 * Mirrors the reviewed routes 1:1 (`08-app-api` applications surface + the
	 * reviewed run-detail route of `05-jobs-api`); mutations are session +
	 * origin + CSRF checked server-side (auth ADR §5) — the bag never weakens
	 * that. Every method names its scope explicitly; there is no module-level
	 * or client-level "current server" anywhere.
	 */
	export interface ApplicationActionsApi {
		/** POST /api/v1/servers/{id}/applications/plan — the pure preview. */
		plan(serverId: string, body: PlanBody): Promise<PlanView>;
		/** POST /api/v1/servers/{id}/applications/{app_id}/install. */
		install(serverId: string, applicationId: string, body: LifecycleSubmitBody): Promise<RunCreated>;
		/** POST /api/v1/servers/{id}/applications/{app_id}/update. */
		update(serverId: string, applicationId: string, body: UpdateSubmitBody): Promise<RunCreated>;
		/** POST /api/v1/servers/{id}/applications/{app_id}/remove. */
		remove(serverId: string, applicationId: string, body: RemoveSubmitBody): Promise<RunCreated>;
		/** GET /api/v1/jobs/runs/{run_id} — replayed state + durable steps. */
		readRun(runId: string): Promise<RunDetail>;
		/** GET /api/v1/approvals/{approval_id} — the bound decision preview. */
		readApproval(approvalId: string): Promise<ApprovalView>;
	}

	/**
	 * Cross-panel linkage seams, each optional and narrowed individually by
	 * design: the composed desktop wires real openers; `null`/incomplete
	 * members fail closed to displayed identity text (never a fake action).
	 */
	export interface ApplicationActionsLinks {
		/** Open one run in the jobs surface. */
		openRun?(runId: string): void;
		/** Open one stored document in the documents surface. */
		openDocument?(docId: string): void;
		/** Open one approval binding in the policy surface (the decision holder). */
		openApproval?(approvalId: string): void;
	}

	interface Props {
		/** Pinned target server — the only target of every call (NT5). */
		serverId: string;
		/** The adopted application this surface acts on. */
		applicationId: string;
		/** Which lifecycle surface to render. */
		mode: LifecycleMode;
		/** The lifecycle API slice (fail-closed when missing/incomplete). */
		api: ApplicationActionsApi;
		/** Optional cross-panel links; unwired members fail closed. */
		links?: ApplicationActionsLinks | null;
		/** Initial recipe document text (ONE canonical recipe document). */
		recipe?: string;
		/** Explicit observed target OS the plan binds (C09 rule 3). */
		targetOs?: string;
		/** Explicit observed target architecture the plan binds. */
		targetArch?: string;
		/** Initial CURRENT recipe text for the update baseline assertion. */
		currentRecipe?: string;
		/** The adoption record-document outcome, for the documents link. */
		recordDoc?: RecordDocView | null;
	}

	let {
		serverId,
		applicationId,
		mode,
		api,
		links = null,
		recipe = '',
		targetOs = '',
		targetArch = '',
		currentRecipe = '',
		recordDoc = null
	}: Props = $props();

	// --- explicit inputs (fail closed when incomplete) -----------------------

	/** Runtime narrowing of the injected API bag; incomplete ⇒ safe state. */
	function readApi(bag: ApplicationActionsApi): ApplicationActionsApi | null {
		const candidate = bag as Partial<ApplicationActionsApi> | null | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		for (const name of ['plan', 'install', 'update', 'remove', 'readRun', 'readApproval'] as const) {
			if (typeof candidate[name] !== 'function') return null;
		}
		return candidate as ApplicationActionsApi;
	}

	/** One linkage seam member, narrowed alone (each fails closed alone). */
	function linkFn(
		name: keyof ApplicationActionsLinks
	): ((...args: string[]) => void) | null {
		const candidate = links as Partial<ApplicationActionsLinks> | null | undefined;
		if (typeof candidate !== 'object' || candidate === null) return null;
		const member = candidate[name];
		return typeof member === 'function' ? member : null;
	}

	// --- producer bounds mirrored for display only (server re-validates) -----

	const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
	/** Fixed poll cadence for the live run view (bounded; torn down on hide). */
	const POLL_MS = 1500;
	/** Run lifecycle states after which nothing new can be learned (C05 rule 2). */
	const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(['succeeded', 'failed', 'cancelled']);

	const usable = $derived(readApi(api) !== null);
	const openRun = $derived(linkFn('openRun'));
	const openDocument = $derived(linkFn('openDocument'));
	const openApproval = $derived(linkFn('openApproval'));
	/** Config sanity: opaque canonical UUID identities and a named target. */
	const configOk = $derived(UUID_RE.test(serverId) && UUID_RE.test(applicationId));

	// --- value-free display helpers (same vocabulary as the jobs surface) -----

	const RUN_STATE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		queued: 'Queued',
		running: 'Running',
		waiting_approval: 'Waiting for approval decision',
		waiting_children: 'Waiting for child runs',
		reconciling: 'Reconciling an unproven outcome',
		succeeded: 'Succeeded',
		failed: 'Failed',
		cancelled: 'Cancelled (verified)'
	});

	const STEP_STATE_TEXT: Readonly<Record<string, string>> = Object.freeze({
		pending: 'Pending',
		running: 'Running',
		succeeded: 'Succeeded',
		failed: 'Failed',
		outcome_unknown: 'Outcome unknown — reconciliation required',
		cancel_requested: 'Cancellation requested — not yet verified',
		cancelled: 'Cancelled (host-verified)'
	});

	/**
	 * The executor status vocabulary of the reviewed `app.*` producers
	 * (`08-install`/`08-update`/`08-remove` receipts), as displayed. None of
	 * these is a success except `succeeded` itself; `rolled_back` and
	 * `refused` are explicitly NOT successes.
	 */
	const EXECUTOR_STATUS_TEXT: Readonly<Record<string, string>> = Object.freeze({
		succeeded: 'Executor status: succeeded',
		failed: 'Executor status: failed',
		refused: 'Executor status: refused — no effect attempted or effect refused by policy',
		rolled_back: 'Executor status: rolled back to the prior version — the update did NOT stick',
		outcome_unknown: 'Executor status: outcome unknown — reconciliation required'
	});

	/** The dispatcher's effect vocabulary (C03/C04 honest-outcome rules). */
	const EFFECT_TEXT: Readonly<Record<string, string>> = Object.freeze({
		executed: 'Effect: executed on the target',
		not_attempted: 'Effect: not attempted',
		unknown: 'Effect: unknown — the target may have changed without a provable outcome'
	});

	function runStateText(state: string | null): string {
		if (state === null) return 'State not recorded yet';
		return RUN_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	function stepStateText(state: string): string {
		return STEP_STATE_TEXT[state] ?? `Recorded state: ${state}`;
	}

	/** Success is EXACTLY the verified terminal success — nothing else (AC 2). */
	function isRunSuccess(state: string | null): boolean {
		return state === 'succeeded';
	}

	function isTerminalRun(state: string | null): boolean {
		return TERMINAL_RUN_STATES.has(state ?? '');
	}

	/** Short display form of an opaque id (full identity always in `title`). */
	function shortId(id: string): string {
		return id.length > 13 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
	}

	function shortHash(hash: string): string {
		return hash.length > 16 ? `${hash.slice(0, 12)}…` : hash;
	}

	// --- instance-local editable inputs ---------------------------------------

	/**
	 * The opener's snapshot is captured ONCE at mount, on purpose: the
	 * catalogue/record texts seed the editing session and a later prop change
	 * of the parent must not silently rewrite the operator's edits (NT5: a
	 * live window never retargets from its parent).
	 */
	function initialRecipe(): string {
		return recipe;
	}

	function initialCurrentRecipe(): string {
		return currentRecipe;
	}

	/** The recipe document this surface plans and submits (ONE document). */
	let recipeText = $state(initialRecipe());
	/** The CURRENT recipe text asserted by the update baseline (C09 rule 4). */
	let currentRecipeText = $state(initialCurrentRecipe());
	/** The EXPLICIT removal data decision — `null` until deliberately chosen. */
	let dataIntent: 'keep' | 'delete' | null = $state(null);
	/**
	 * The operator's declared-backup decision (C09 rule 4/5). Deliberately
	 * `false` until confirmed: a backup claim is the operator's, never the
	 * UI's; the producer refuses data-affecting effects when its own gate
	 * requires one and this stays false (`backup_required` refusal).
	 */
	let backupConfirmed = $state(false);

	// --- plan + submit + run state (all instance-local; no shared cache) ------

	type Phase = 'input' | 'planning' | 'planned' | 'submitting' | 'submitted';

	let phase = $state<Phase>('input');
	let plan: PlanView | null = $state(null);
	let planProblem: string | null = $state(null);
	let planRefused = $state(false);
	let submitProblem: string | null = $state(null);
	let submittedRunId: string | null = $state(null);
	let deduplicated = $state(false);
	let run: RunDetail | null = $state(null);
	let runProblem: string | null = $state(null);
	let runRefused = $state(false);
	let approval: ApprovalView | null = $state(null);
	/** Monotonic guards: late/superseded responses never paint (AC 3). */
	let planEpoch = 0;
	let runEpoch = 0;
	let disposed = false;
	/** The live-poll handle; null while no non-terminal run is watched. */
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const busy = $derived(phase === 'planning' || phase === 'submitting');
	/** The recipe bytes are locked while a plan built from them is shown. */
	const recipeLocked = $derived(plan !== null || phase === 'submitting' || phase === 'submitted');
	const canPreview = $derived(
		usable &&
			configOk &&
			!busy &&
			recipeText.trim().length > 0 &&
			targetOs.trim().length > 0 &&
			targetArch.trim().length > 0 &&
			(mode !== 'update' || currentRecipeText.trim().length > 0)
	);
	/**
	 * Submit enablement is the contract made visible: the data decision has NO
	 * default, so an unmade decision cannot start a removal (C09 rule 5).
	 */
	const canSubmit = $derived(
		usable &&
			configOk &&
			plan !== null &&
			phase === 'planned' &&
			(mode !== 'remove' || dataIntent !== null)
	);

	/** The newest durable step of the watched run (receipts are append-only). */
	const latestStep = $derived.by<StepView | null>(() => {
		if (run === null) return null;
		if (run.steps.length === 0) return null;
		return run.steps[run.steps.length - 1];
	});

	/** The step receipt, narrowed to the safe producer fields we display. */
	interface ReceiptFacts {
		status: string | null;
		effect: string | null;
		reason: string | null;
		allowed: boolean | null;
		approvalId: string | null;
		approvalCode: string | null;
		approvalSpent: boolean | null;
		payloadHash: string | null;
		policyRevision: number | null;
	}

	function receiptFacts(step: StepView | null): ReceiptFacts {
		const empty: ReceiptFacts = {
			status: null,
			effect: null,
			reason: null,
			allowed: null,
			approvalId: null,
			approvalCode: null,
			approvalSpent: null,
			payloadHash: null,
			policyRevision: null
		};
		if (step === null || typeof step.receipt !== 'object' || step.receipt === null) return empty;
		const raw = step.receipt as Record<string, unknown>;
		const str = (key: string): string | null =>
			typeof raw[key] === 'string' && raw[key] !== '' ? (raw[key] as string) : null;
		const num = (key: string): number | null =>
			typeof raw[key] === 'number' ? (raw[key] as number) : null;
		const bool = (key: string): boolean | null =>
			typeof raw[key] === 'boolean' ? (raw[key] as boolean) : null;
		return {
			status: str('status'),
			effect: str('effect'),
			reason: str('reason'),
			allowed: bool('allowed'),
			approvalId: str('approval_id'),
			approvalCode: str('approval_code'),
			approvalSpent: bool('approval_spent'),
			payloadHash: str('payload_hash'),
			policyRevision: num('policy_revision')
		};
	}

	const facts = $derived(receiptFacts(latestStep));

	// --- error mapping (C01 rule 3 envelopes; display-only) -------------------

	interface Problem {
		code: string | null;
		message: string;
	}

	function problemOf(error: unknown, fallback: string): Problem {
		if (typeof error === 'object' && error !== null) {
			const candidate = error as { code?: unknown; message?: unknown };
			return {
				code: typeof candidate.code === 'string' ? candidate.code : null,
				message:
					typeof candidate.message === 'string' && candidate.message !== ''
						? candidate.message
						: fallback
			};
		}
		return { code: null, message: fallback };
	}

	// --- actions (every outgoing call names its pinned scope) -----------------

	/** Preview the deterministic policy-bound plan (pure route; no effect). */
	async function previewPlan(): Promise<void> {
		const client = readApi(api);
		if (client === null || !configOk || busy) return;
		const epoch = ++planEpoch;
		phase = 'planning';
		planProblem = null;
		planRefused = false;
		try {
			const body: PlanBody = {
				application_id: applicationId,
				recipe: recipeText,
				target_os: targetOs.trim(),
				target_arch: targetArch.trim()
			};
			const view = await client.plan(serverId, body);
			if (disposed || epoch !== planEpoch) return;
			// Identity guard: only THIS server's + application's plan may paint.
			if (view.server_id !== serverId || view.application_id !== applicationId) {
				planProblem = 'The plan answer names a different target; nothing was bound.';
				planRefused = true;
				phase = 'input';
				return;
			}
			plan = view;
			phase = 'planned';
		} catch (error) {
			if (disposed || epoch !== planEpoch) return;
			const problem = problemOf(error, 'The plan could not be built.');
			planProblem = problem.code
				? `${problem.message} (code: ${problem.code})`
				: problem.message;
			planRefused = true;
			phase = 'input';
		}
	}

	/**
	 * The data intent is not part of the plan digest — it travels inside the
	 * digest-bound step payload at submit time, so binding `bind:group` here
	 * is the whole decision surface: nothing preselects it, nothing infers it.
	 */

	/** Submit the bound plan as ONE durable `app.*` intent (C05 rule 5). */
	async function submitAction(): Promise<void> {
		const client = readApi(api);
		if (client === null || !configOk || plan === null || phase !== 'planned') return;
		if (mode === 'remove' && dataIntent === null) return; // no inferred delete
		const epoch = ++runEpoch;
		phase = 'submitting';
		submitProblem = null;
		runRefused = false;
		try {
			const runId = crypto.randomUUID();
			let created: RunCreated;
			if (mode === 'install') {
				const body: LifecycleSubmitBody = {
					run_id: runId,
					recipe: recipeText,
					target_os: targetOs.trim(),
					target_arch: targetArch.trim(),
					backup_confirmed: backupConfirmed
				};
				created = await client.install(serverId, applicationId, body);
			} else if (mode === 'update') {
				const body: UpdateSubmitBody = {
					run_id: runId,
					recipe: recipeText,
					target_os: targetOs.trim(),
					target_arch: targetArch.trim(),
					backup_confirmed: backupConfirmed,
					current_recipe: currentRecipeText
				};
				created = await client.update(serverId, applicationId, body);
			} else {
				const body: RemoveSubmitBody = {
					run_id: runId,
					recipe: recipeText,
					target_os: targetOs.trim(),
					target_arch: targetArch.trim(),
					backup_confirmed: backupConfirmed,
					data_intent: dataIntent as 'keep' | 'delete'
				};
				created = await client.remove(serverId, applicationId, body);
			}
			if (disposed || epoch !== runEpoch) return;
			// Identity guard: the created run must be ours, on OUR pinned server.
			if (created.run.server_id !== serverId) {
				submitProblem = 'The submit answer names a different server; nothing is watched.';
				runRefused = true;
				phase = 'planned';
				return;
			}
			submittedRunId = created.run.run_id;
			deduplicated = created.deduplicated === true;
			await adoptRunDetail(created.run.run_id, epoch, {
				state: created.run.state,
				cancelRequestedAt: created.run.cancel_requested_at
			});
		} catch (error) {
			if (disposed || epoch !== runEpoch) return;
			const problem = problemOf(error, 'The intent could not be submitted.');
			submitProblem = problem.code
				? `${problem.message} (code: ${problem.code})`
				: problem.message;
			runRefused = true;
			phase = 'planned';
		}
	}

	/**
	 * Adopt one run-detail answer for the watched run. Identity is checked
	 * against the pinned scope AND the submitted run id before anything paints
	 * (C01 rule 2; a foreign run is refused, never rendered).
	 */
	async function adoptRunDetail(
		runId: string,
		epoch: number,
		fallback: { state: string | null; cancelRequestedAt: string | null }
	): Promise<void> {
		const client = readApi(api);
		if (client === null) return;
		try {
			const detail = await client.readRun(runId);
			if (disposed || epoch !== runEpoch) return;
			if (detail.run_id !== runId || detail.server_id !== serverId) {
				runProblem = 'The run answer names a different run or server; nothing is shown.';
				runRefused = true;
				stopPolling();
				return;
			}
			run = detail;
			runProblem = null;
			runRefused = false;
			phase = 'submitted';
			await refreshApproval(epoch);
			if (!isTerminalRun(detail.state)) armPolling(runId, epoch);
			else stopPolling();
		} catch (error) {
			if (disposed || epoch !== runEpoch) return;
			// The submit DID create a durable intent; only the view is stale.
			run = {
				run_id: runId,
				server_id: serverId,
				parent_run_id: null,
				depth: 0,
				profile_revision: 0,
				prompt_revision: 0,
				model_profile: '',
				requested_grants: [],
				label: '',
				state: fallback.state,
				cancel_requested_at: fallback.cancelRequestedAt,
				submitted_at: '',
				updated_at: '',
				steps: []
			};
			const problem = problemOf(error, 'The run state could not be read.');
			runProblem = problem.code
				? `${problem.message} (code: ${problem.code}) — retry below; the intent is durable.`
				: `${problem.message} — retry below; the intent is durable.`;
			runRefused = false;
			phase = 'submitted';
			stopPolling();
		}
	}

	/** Manual refresh path after a read failure (re-arms polling when open). */
	async function refreshRun(): Promise<void> {
		const epoch = ++runEpoch;
		if (submittedRunId === null) return;
		runProblem = null;
		await adoptRunDetail(submittedRunId, epoch, {
			state: run?.state ?? null,
			cancelRequestedAt: run?.cancel_requested_at ?? null
		});
	}

	function armPolling(runId: string, epoch: number): void {
		stopPolling();
		pollTimer = setInterval(() => {
			if (disposed) {
				stopPolling();
				return;
			}
			void adoptRunDetail(runId, epoch, {
				state: run?.state ?? null,
				cancelRequestedAt: run?.cancel_requested_at ?? null
			});
		}, POLL_MS);
	}

	function stopPolling(): void {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	/** The approval binding behind a waiting/decided gate (display only). */
	async function refreshApproval(epoch: number): Promise<void> {
		const client = readApi(api);
		const id = facts.approvalId;
		if (client === null || id === null || !UUID_RE.test(id)) return;
		try {
			const view = await client.readApproval(id);
			if (disposed || epoch !== runEpoch) return;
			if (view.approval_id !== id || view.server_id !== serverId) return;
			approval = view;
		} catch {
			if (disposed || epoch !== runEpoch) return;
			// The binding facts from the receipt stay displayed; the decision
			// preview is simply unavailable right now (C08 rule 6 honesty).
			approval = null;
		}
	}

	// --- linkage seams (fail closed when unwired) -----------------------------

	function openRunLink(): void {
		if (submittedRunId === null || openRun === null) return;
		openRun(submittedRunId);
	}

	function openDocumentLink(): void {
		const docId = recordDoc?.doc_id ?? null;
		if (docId === null || openDocument === null) return;
		openDocument(docId);
	}

	function openApprovalLink(): void {
		if (facts.approvalId === null || openApproval === null) return;
		openApproval(facts.approvalId);
	}

	// --- mount cycle (the $effect teardown is the release path) ---------------

	$effect(() => {
		disposed = false;
		return () => {
			disposed = true;
			stopPolling();
		};
	});

	onDestroy(() => {
		disposed = true;
		stopPolling();
	});

	const MODE_TITLE: Record<LifecycleMode, string> = {
		install: 'Install application',
		update: 'Update application',
		remove: 'Remove application'
	};
</script>

<div class="mai-appactions" data-testid="appactions-root" data-mode={mode}>
	{#if !usable || !configOk}
		<div class="mai-appactions__state mai-appactions__state--error" role="alert" data-testid="appactions-config-error">
			<p>
				This surface was opened without its complete lifecycle service, or outside a
				pinned (server, application) identity. Nothing can be planned or submitted here.
			</p>
		</div>
	{:else}
		<header class="mai-appactions__header">
			<h3 class="mai-appactions__title" data-testid="appactions-title">{MODE_TITLE[mode]}</h3>
			<p class="mai-appactions__meta">
				<span data-testid="appactions-scope">
					Server {shortId(serverId)} · application {shortId(applicationId)}
				</span>
				<span title={`${serverId} / ${applicationId}`}>
					Pinned scope — every request below names exactly this target.
				</span>
			</p>
		</header>

		<!-- -------------------------------------------------- recipe + target -->
		<section class="mai-appactions__section" aria-label="Recipe and target" data-testid="appactions-input">
			<div class="mai-appactions__target">
				<span data-testid="appactions-target-os">Target OS: {targetOs === '' ? '(not named)' : targetOs}</span>
				<span data-testid="appactions-target-arch">Target arch: {targetArch === '' ? '(not named)' : targetArch}</span>
			</div>
			<label class="mai-appactions__field">
				<span class="mai-appactions__field-label">Recipe document (ONE canonical document)</span>
				<textarea
					class="mai-appactions__textarea"
					bind:value={recipeText}
					disabled={recipeLocked}
					rows={10}
					spellcheck="false"
					data-testid="recipe-input"
				></textarea>
			</label>
			{#if mode === 'update'}
				<label class="mai-appactions__field">
					<span class="mai-appactions__field-label">
						CURRENT recipe (baseline assertion — verified against the deployed bytes before any
						effect)
					</span>
					<textarea
						class="mai-appactions__textarea"
						bind:value={currentRecipeText}
						disabled={recipeLocked}
						rows={10}
						spellcheck="false"
						data-testid="current-recipe-input"
					></textarea>
				</label>
			{/if}
			<div class="mai-appactions__actions">
				<Button
					variant="secondary"
					onclick={previewPlan}
					disabled={!canPreview}
					loading={phase === 'planning'}
					data-testid="plan-preview"
				>
					Preview plan
				</Button>
				{#if recipeLocked}
					<span class="mai-appactions__note" data-testid="recipe-locked">
						The recipe is bound to the previewed plan (plan hash); edit after this
						lifecycle action finishes.
					</span>
				{/if}
			</div>
			{#if planRefused && planProblem !== null}
				<div class="mai-appactions__state mai-appactions__state--error" role="alert" data-testid="plan-error" >
					<p>{planProblem}</p>
				</div>
			{/if}
		</section>

		<!-- -------------------------------------------------------- plan view -->
		{#if plan !== null}
			<section class="mai-appactions__section" aria-label="Bound plan" data-testid="plan-view">
				<h4>Bound plan (deterministic; policy-bound)</h4>
				<dl class="mai-appactions__facts">
					<dt>Project</dt>
					<dd data-testid="plan-project">{plan.project_name}</dd>
					<dt>Image</dt>
					<dd data-testid="plan-image" class="mai-appactions__mono">{plan.image}</dd>
					<dt>Compose path</dt>
					<dd>
						<span data-testid="plan-compose-path" class="mai-appactions__mono">{plan.compose_path}</span>
						<span class="mai-appactions__note"> compose {shortHash(plan.compose_sha256)} · recipe {shortHash(plan.recipe_sha256)} · plan {shortHash(plan.plan_sha256)}</span>
					</dd>
					<dt>Target</dt>
					<dd data-testid="plan-target">{plan.target_os} / {plan.target_arch}</dd>
					{#if plan.directories.length > 0}
						<dt>Directories</dt>
						<dd data-testid="plan-directories">{plan.directories.join(', ')}</dd>
					{/if}
					{#if plan.ports.length > 0}
						<dt>Ports</dt>
						<dd data-testid="plan-ports">
							{#each plan.ports as port, index (index)}
								<span class="mai-appactions__mono" data-testid="plan-port">
									{port.host} → {port.container}/{port.protocol}</span>{#if index < plan.ports.length - 1},{/if}
							{/each}
						</dd>
					{/if}
					{#if plan.volumes.length > 0}
						<dt>Persistent data (volumes)</dt>
						<dd data-testid="plan-volumes">
							{#each plan.volumes as volume, index (index)}
								<span data-testid="plan-volume">
									{volume.name}: container {volume.container_path} ⇔ host <span class="mai-appactions__mono">{volume.host_path}</span></span>{#if index < plan.volumes.length - 1}; {/if}
							{/each}
						</dd>
					{:else}
						<dt>Persistent data (volumes)</dt>
						<dd data-testid="plan-volumes">None declared by the recipe.</dd>
					{/if}
					{#if plan.credentials.length > 0 || plan.credentials_path !== null}
						<dt>Credential references</dt>
						<dd data-testid="plan-credentials">
							{plan.credentials.join(', ')}{plan.credentials_path !== null
								? `${plan.credentials.length > 0 ? ' — ' : ''}file: ${plan.credentials_path}`
								: ''}
							<span class="mai-appactions__note"> (references only; values resolve server-side)</span>
						</dd>
					{/if}
					{#if plan.health_probe !== null}
						<dt>Health probe</dt>
						<dd data-testid="plan-health">
							{plan.health_probe.kind} {plan.health_probe.path} on container port
							{plan.health_probe.port}, expect {plan.health_probe.expected_status} (every
							{plan.health_probe.interval_seconds}s, timeout {plan.health_probe.timeout_seconds}s,
							{plan.health_probe.retries} retries, start period {plan.health_probe.start_period_seconds}s)
						</dd>
					{:else}
						<dt>Health probe</dt>
						<dd data-testid="plan-health">None declared — the executor cannot verify health.</dd>
					{/if}
					{#if plan.prerequisites.length > 0}
						<dt>Prerequisites</dt>
						<dd data-testid="plan-prerequisites">{plan.prerequisites.join(', ')}</dd>
					{/if}
					<dt>Required grants</dt>
					<dd data-testid="plan-grants">
						{plan.required_grants.length > 0 ? plan.required_grants.join(', ') : '(none)'}
						<span class="mai-appactions__note">
							— policy and approvals are enforced worker-side on this exact intent (G2 gate).
						</span>
					</dd>
					<dt>Operations</dt>
					<dd data-testid="plan-operations">
						{#each plan.operations as operation, index (index)}
							<span data-testid="plan-operation">{operation.op}: {operation.subject}</span>{#if index < plan.operations.length - 1}; {/if}
						{/each}
					</dd>
					{#if plan.limitations.length > 0}
						<dt>Limitations</dt>
						<dd data-testid="plan-limitations">{plan.limitations.join(' ')}</dd>
					{/if}
				</dl>

				<!-- ---------------------------------------------- decision inputs -->
				<div class="mai-appactions__decision" data-testid="appactions-decision">
					<label class="mai-appactions__check">
						<input
							type="checkbox"
							bind:checked={backupConfirmed}
							disabled={phase === 'submitting' || phase === 'submitted'}
							data-testid="backup-check"
						/>
						A verified backup of this application's persistent data exists
						<span class="mai-appactions__note">(declared by you; the producer refuses data-affecting
						effects that require one and stay unconfirmed)</span>
					</label>
					{#if mode === 'remove'}
						<fieldset class="mai-appactions__fieldset" data-testid="data-intent-group">
							<legend>Persistent data decision (explicit — nothing is preselected)</legend>
							<label class="mai-appactions__check">
								<input
									type="radio"
									name="mai-data-intent"
									value="keep"
									bind:group={dataIntent}
									disabled={phase === 'submitting' || phase === 'submitted'}
									data-testid="data-keep"
								/>
								Keep the persistent data on the host (retained at the planned locations)
							</label>
							<label class="mai-appactions__check">
								<input
									type="radio"
									name="mai-data-intent"
									value="delete"
									bind:group={dataIntent}
									disabled={phase === 'submitting' || phase === 'submitted'}
									data-testid="data-delete"
								/>
								Record a delete intent for the persistent data
							</label>
							<p class="mai-appactions__note" data-testid="remove-two-tier-note">
								Two-tier boundary: this removal stops the containers and tombstones the canonical
								record. It NEVER deletes persistent data — a recorded delete intent stays a
								RECORDED decision; the data survives until the separately authorized tier-2 purge
								(one-use approval bound to the exact deletion manifest).
							</p>
						</fieldset>
					{/if}
					<div class="mai-appactions__actions">
						<Button
							variant={mode === 'remove' ? 'danger' : 'primary'}
							onclick={submitAction}
							disabled={!canSubmit}
							loading={phase === 'submitting'}
							data-testid="submit-action"
						>
							{mode === 'install' ? 'Submit install intent' : mode === 'update' ? 'Submit update intent' : 'Submit removal intent'}
						</Button>
						{#if mode === 'remove' && dataIntent === null}
							<span class="mai-appactions__note" data-testid="data-intent-required">
								Choose keep or delete — the decision is explicit; an unmade decision cannot be
								submitted.
							</span>
						{/if}
					</div>
					{#if runRefused && submitProblem !== null}
						<div class="mai-appactions__state mai-appactions__state--error" role="alert" data-testid="submit-error">
							<p>{submitProblem}</p>
						</div>
					{/if}
				</div>
			</section>
		{/if}

		<!-- --------------------------------------------------------- run view -->
		{#if phase === 'submitted' && submittedRunId !== null}
			<section class="mai-appactions__section" aria-label="Submitted intent" data-testid="run-view">
				<h4>Durable intent</h4>
				<p class="mai-appactions__meta">
					<span data-testid="run-id" title={submittedRunId}>Run {shortId(submittedRunId)}</span>
					{#if deduplicated}
						<span class="mai-appactions__badge mai-appactions__badge--neutral" data-testid="run-dedup">
							Duplicate submit — attached to the existing run
						</span>
					{/if}
				</p>
				{#if run !== null}
					<p class="mai-appactions__runline">
						<span
							class="mai-appactions__badge"
							class:mai-appactions__badge--ok={isRunSuccess(run.state)}
							class:mai-appactions__badge--danger={run.state === 'failed'}
							class:mai-appactions__badge--warn={!isRunSuccess(run.state) && run.state !== 'failed'}
							data-testid="run-state"
							data-state={run.state ?? 'unknown'}
						>
							{runStateText(run.state)}
						</span>
						{#if run.cancel_requested_at !== null && run.state !== 'cancelled'}
							<span class="mai-appactions__badge mai-appactions__badge--warn" data-testid="run-cancel-requested">
								Cancellation requested — not yet verified
							</span>
						{/if}
					</p>
					<dl class="mai-appactions__facts">
						<dt>Requested grants</dt>
						<dd data-testid="run-grants">{run.requested_grants.length > 0 ? run.requested_grants.join(', ') : '(none)'}</dd>
					</dl>
					{#if runProblem !== null}
						<div
							class="mai-appactions__state mai-appactions__state--error"
							role="alert"
							data-testid="run-error"
						>
							<p>{runProblem}</p>
							<Button variant="secondary" onclick={refreshRun} data-testid="run-refresh">
								Refresh run state
							</Button>
						</div>
					{/if}
					{#if latestStep !== null}
						<div class="mai-appactions__step" data-testid="step-row" data-step-state={latestStep.state}>
							<span
								class="mai-appactions__badge"
								class:mai-appactions__badge--ok={latestStep.state === 'succeeded'}
								class:mai-appactions__badge--danger={latestStep.state === 'failed' ||
									latestStep.state === 'outcome_unknown'}
								class:mai-appactions__badge--warn={latestStep.state === 'running' ||
									latestStep.state === 'pending' ||
									latestStep.state === 'cancel_requested'}
								class:mai-appactions__badge--neutral={latestStep.state === 'cancelled'}
								data-testid="step-state"
							>
								{stepStateText(latestStep.state)}
							</span>
							<span class="mai-appactions__note" data-testid="step-capability">{latestStep.capability_id}</span>
							<dl class="mai-appactions__facts" data-testid="step-receipt">
								{#if facts.status !== null}
									<dt>Executor</dt>
									<dd>
										<span data-testid="receipt-status">{EXECUTOR_STATUS_TEXT[facts.status] ?? `Executor status: ${facts.status}`}</span>
									</dd>
								{/if}
								{#if facts.effect !== null}
									<dt>Effect</dt>
									<dd data-testid="receipt-effect">{EFFECT_TEXT[facts.effect] ?? `Effect: ${facts.effect}`}</dd>
								{/if}
								{#if facts.reason !== null}
									<dt>Reason</dt>
									<dd data-testid="receipt-reason" class="mai-appactions__mono">{facts.reason}</dd>
								{/if}
								{#if facts.allowed !== null}
									<dt>Policy decision</dt>
									<dd data-testid="receipt-allowed">{facts.allowed ? 'allowed' : 'not allowed'}</dd>
								{/if}
								{#if facts.payloadHash !== null}
									<dt>Bound payload</dt>
									<dd>
										<span data-testid="receipt-payload-hash" class="mai-appactions__mono" title={facts.payloadHash}>
											{shortHash(facts.payloadHash)}
										</span>
										{facts.policyRevision !== null ? ` · policy r${facts.policyRevision}` : ''}
									</dd>
								{/if}
							</dl>
						{#if run.state === 'waiting_approval' && facts.approvalId === null}
							<p class="mai-appactions__note" data-testid="approval-wait">
								The effect is parked until an approval decision exists, bound to payload
								{#if facts.payloadHash !== null}
									<span class="mai-appactions__mono" title={facts.payloadHash}>{shortHash(facts.payloadHash)}</span>
								{/if}{facts.policyRevision !== null ? ` at policy r${facts.policyRevision}` : ''}.
								Create that decision in the capability-policy surface — it authorizes exactly
								this bound operation, nothing else.
							</p>
						{/if}
						{#if facts.approvalId !== null}
								<div class="mai-appactions__approval" data-testid="receipt-approval">
									<p class="mai-appactions__meta">
										Approval binding
										<span class="mai-appactions__mono" data-testid="approval-id" title={facts.approvalId}>
											{shortId(facts.approvalId)}</span>
										{#if facts.approvalSpent === true}
											<span class="mai-appactions__badge mai-appactions__badge--neutral" data-testid="approval-spent">
												decision consumed
											</span>
										{:else if facts.approvalCode !== null}
											<span class="mai-appactions__badge mai-appactions__badge--warn" data-testid="approval-code">
												{facts.approvalCode}
											</span>
										{/if}
									</p>
									{#if approval !== null}
										<dl class="mai-appactions__facts" data-testid="approval-view">
											<dt>Status</dt>
											<dd data-testid="approval-status">{approval.status}</dd>
											<dt>Payload hash</dt>
											<dd><span class="mai-appactions__mono" title={approval.payload_hash}>{shortHash(approval.payload_hash)}</span></dd>
											<dt>Policy revision</dt>
											<dd data-testid="approval-policy-revision">{approval.policy_revision}</dd>
											<dt>Expires</dt>
											<dd data-testid="approval-expires">{approval.expires_at}</dd>
										</dl>
									{/if}
									{#if openApproval !== null}
										<Button variant="secondary" onclick={openApprovalLink} data-testid="approval-open">
											Open in policy surface…
										</Button>
									{:else}
										<p class="mai-appactions__note" data-testid="approval-link-unavailable">
											The policy surface is not wired into this composition; decide through the
											capability-policy window.
										</p>
									{/if}
								</div>
							{/if}
							{#if mode === 'remove' && dataIntent !== null}
								<p class="mai-appactions__note" data-testid="data-intent-recorded">
									{#if dataIntent === 'delete'}
										Recorded data decision: <strong>delete intent recorded</strong> — the persistent
										data was NOT deleted by this removal; it survives until the separately
										authorized tier-2 purge (one-use manifest-bound approval).
									{:else}
										Recorded data decision: <strong>keep</strong> — the persistent data is retained
										on the host at the planned locations.
									{/if}
								</p>
							{/if}
						</div>
					{/if}
					<div class="mai-appactions__actions">
						{#if openRun !== null}
							<Button variant="secondary" onclick={openRunLink} data-testid="run-open">
								Open run in jobs surface…
							</Button>
						{:else}
							<span class="mai-appactions__note" data-testid="run-link-unavailable">
								The jobs surface is not wired into this composition; follow run
								<span class="mai-appactions__mono">{shortId(submittedRunId)}</span> there.
							</span>
						{/if}
					</div>
				{/if}
			</section>
		{/if}

		<!-- ------------------------------------------------- record doc link -->
		{#if recordDoc !== null}
			<section class="mai-appactions__section" aria-label="Canonical record document" data-testid="record-doc">
				{#if recordDoc.status === 'documented' && recordDoc.doc_id}
					<p class="mai-appactions__meta">
						Canonical record document
						<span class="mai-appactions__mono" data-testid="record-doc-id" title={recordDoc.doc_id}>
							{shortId(recordDoc.doc_id)}</span>
					</p>
					{#if openDocument !== null}
						<Button variant="secondary" onclick={openDocumentLink} data-testid="record-doc-open">
							Open record document…
						</Button>
					{:else}
						<p class="mai-appactions__note" data-testid="record-doc-link-unavailable">
							The documents surface is not wired into this composition; open document
							<span class="mai-appactions__mono">{shortId(recordDoc.doc_id)}</span> there.
						</p>
					{/if}
				{:else}
					<p class="mai-appactions__note" data-testid="record-doc-pending">
						Canonical record document: pending{recordDoc.reason !== null && recordDoc.reason !== undefined && recordDoc.reason !== ''
							? ` (${recordDoc.reason})`
							: ''} — the committed record is not rolled back for it.
					</p>
				{/if}
			</section>
		{/if}
	{/if}
</div>

<style>
	.mai-appactions {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-4, 16px);
		padding: var(--mai-space-4, 16px);
		color: var(--mai-color-text);
		font-size: var(--mai-text-sm, 0.875rem);
		line-height: var(--mai-leading-normal, 1.5);
		min-width: 0;
	}

	.mai-appactions__header {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 4px);
	}

	.mai-appactions__title {
		margin: 0;
		font-size: var(--mai-text-lg, 1.125rem);
		font-weight: var(--mai-weight-semibold, 600);
	}

	.mai-appactions__meta {
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-2, 8px);
		align-items: baseline;
		color: var(--mai-color-text-muted);
	}

	.mai-appactions__section {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-3, 12px);
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-radius: var(--mai-radius-md, 6px);
		padding: var(--mai-space-3, 12px);
		background: var(--mai-color-surface);
		min-width: 0;
	}

	.mai-appactions__section h4 {
		margin: 0;
		font-size: var(--mai-text-base, 1rem);
		font-weight: var(--mai-weight-semibold, 600);
	}

	.mai-appactions__target {
		display: flex;
		gap: var(--mai-space-4, 16px);
		color: var(--mai-color-text-muted);
	}

	.mai-appactions__field {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 4px);
		min-width: 0;
	}

	.mai-appactions__field-label {
		font-weight: var(--mai-weight-medium, 500);
		color: var(--mai-color-text-muted);
	}

	.mai-appactions__textarea {
		width: 100%;
		resize: vertical;
		font-family: var(--mai-font-mono, monospace);
		font-size: var(--mai-text-xs, 0.75rem);
		padding: var(--mai-space-2, 8px);
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 4px);
		background: var(--mai-color-surface-raised);
		color: inherit;
	}

	.mai-appactions__textarea:disabled {
		background: var(--mai-status-neutral-bg);
		color: var(--mai-status-neutral-text);
	}

	.mai-appactions__actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-2, 8px);
		align-items: center;
	}

	.mai-appactions__facts {
		margin: 0;
		display: grid;
		grid-template-columns: minmax(9rem, max-content) 1fr;
		gap: var(--mai-space-1, 4px) var(--mai-space-3, 12px);
		min-width: 0;
	}

	.mai-appactions__facts dt {
		font-weight: var(--mai-weight-medium, 500);
		color: var(--mai-color-text-muted);
	}

	.mai-appactions__facts dd {
		margin: 0;
		min-width: 0;
		overflow-wrap: anywhere;
	}

	.mai-appactions__mono {
		font-family: var(--mai-font-mono, monospace);
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-appactions__note {
		color: var(--mai-color-text-muted);
		font-size: var(--mai-text-xs, 0.75rem);
	}

	.mai-appactions__decision {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 8px);
		border-top: var(--mai-border-width, 1px) solid var(--mai-color-border);
		padding-top: var(--mai-space-3, 12px);
	}

	.mai-appactions__fieldset {
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 4px);
		padding: var(--mai-space-2, 8px);
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-1, 4px);
		min-width: 0;
	}

	.mai-appactions__fieldset legend {
		padding: 0 var(--mai-space-1, 4px);
		font-weight: var(--mai-weight-medium, 500);
	}

	.mai-appactions__check {
		display: flex;
		gap: var(--mai-space-2, 8px);
		align-items: baseline;
	}

	.mai-appactions__runline {
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mai-space-2, 8px);
		align-items: center;
	}

	.mai-appactions__step {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 8px);
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 4px);
		padding: var(--mai-space-2, 8px);
	}

	.mai-appactions__approval {
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 8px);
	}

	.mai-appactions__badge {
		display: inline-block;
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 4px);
		padding: 0 var(--mai-space-2, 8px);
		white-space: nowrap;
	}

	.mai-appactions__badge--ok {
		border-color: var(--mai-status-success-border);
		color: var(--mai-status-success-text);
		background: var(--mai-status-success-bg);
	}

	.mai-appactions__badge--danger {
		border-color: var(--mai-status-danger-border);
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
	}

	.mai-appactions__badge--warn {
		border-color: var(--mai-status-warning-border);
		color: var(--mai-status-warning-text);
		background: var(--mai-status-warning-bg);
	}

	.mai-appactions__badge--neutral {
		border-color: var(--mai-status-neutral-border);
		color: var(--mai-status-neutral-text);
		background: var(--mai-status-neutral-bg);
	}

	.mai-appactions__state {
		border: var(--mai-border-width, 1px) solid var(--mai-color-border);
		border-radius: var(--mai-radius-sm, 4px);
		padding: var(--mai-space-2, 8px);
		display: flex;
		flex-direction: column;
		gap: var(--mai-space-2, 8px);
	}

	.mai-appactions__state p {
		margin: 0;
	}

	.mai-appactions__state--error {
		border-color: var(--mai-status-danger-border);
		color: var(--mai-status-danger-text);
		background: var(--mai-status-danger-bg);
	}
</style>
